"""Durable Drive version journal and cancellable read-only ingestion."""
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

from PySide6.QtCore import QThread, Signal
from .drive_config import DriveBinding
from .google_drive import DriveClient, DriveError, DriveAuthRequired
from .media import selected_extensions

CHINA_TIME = timezone(timedelta(hours=8))


def version_of(file):
    # A renamed/replaced cloud copy has a new identity/version; content dedup is later.
    return hashlib.sha256(json.dumps([file.get(k) for k in
        ('id', 'version', 'modifiedTime', 'size', 'md5Checksum')]).encode()).hexdigest()


def safe_name(name):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).rstrip(' .')
    if not value or value.split('.')[0].upper() in {'CON','PRN','AUX','NUL', *('COM'+str(i) for i in range(1,10)), *('LPT'+str(i) for i in range(1,10))}:
        value = '_' + value
    path = Path(value)
    return path.stem[:140] + path.suffix[:12]


class DriveJournal:
    def __init__(self, root):
        self.path = Path(root) / 'drive_ingest.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS files (
                identity TEXT PRIMARY KEY, target TEXT NOT NULL, binding TEXT NOT NULL,
                cloud_id TEXT NOT NULL, version TEXT NOT NULL, path TEXT NOT NULL,
                state TEXT NOT NULL, sha256 TEXT, duplicate INTEGER NOT NULL DEFAULT 0)''')
            db.execute('''CREATE TABLE IF NOT EXISTS scan_cursor (
                target TEXT NOT NULL, binding TEXT NOT NULL, day TEXT NOT NULL,
                PRIMARY KEY(target,binding))''')

    def resume_day(self, target, today):
        binding = self.identity(target, {'id': ''})[1]
        with self.connect() as db:
            row = db.execute('SELECT day FROM scan_cursor WHERE target=? AND binding=?',
                (target.key,binding)).fetchone()
        return min(date.fromisoformat(row[0]), today) if row else today

    def save_scan_day(self, target, day):
        binding = self.identity(target, {'id': ''})[1]
        with self.connect() as db:
            db.execute('INSERT INTO scan_cursor VALUES (?,?,?) ON CONFLICT(target,binding) DO UPDATE SET day=excluded.day',
                (target.key,binding,day.isoformat()))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def identity(target, file):
        binding = str(Path(target.account.target_dir).resolve()) + '|' + DriveBinding.read(target.account.drive_binding).folder_id
        return hashlib.sha256(json.dumps([target.key, binding, file['id'], version_of(file)]).encode()).hexdigest(), binding

    def entry(self, identity):
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute('SELECT * FROM files WHERE identity=?', (identity,)).fetchone()
            return dict(row) if row else None

    def reserve(self, target, file):
        identity, binding = self.identity(target, file)
        existing = self.entry(identity)
        if existing:
            return existing
        directory = Path(target.account.target_dir).resolve()
        path = directory / safe_name(file['name'])
        if path.exists():
            path = path.with_name(path.stem + '_' + identity[:12] + path.suffix)
        # Persist intended final name BEFORE any bytes are written, for crash recovery.
        with self.connect() as db:
            db.execute('INSERT INTO files VALUES (?,?,?,?,?,?,?,NULL,0)',
                (identity, target.key, binding, file['id'], version_of(file), str(path), 'FETCHING'))
        return self.entry(identity)

    def complete(self, identity, digest, duplicate):
        with self.connect() as db:
            db.execute("UPDATE files SET state='DOWNLOADED',sha256=?,duplicate=? WHERE identity=?",
                (digest, int(duplicate), identity))

    def origins(self, path, digest):
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute("SELECT * FROM files WHERE path=? AND sha256=? AND state='DOWNLOADED'",
                (str(Path(path).resolve()), digest))]

    def mark_duplicate(self, target, path, digest):
        with self.connect() as db:
            db.execute("UPDATE files SET duplicate=1 WHERE target=? AND path=? AND sha256=? AND state='DOWNLOADED'",
                (target,str(Path(path).resolve()),digest))


def file_hashes(path, stopped=lambda: False):
    sha, md5 = hashlib.sha256(), hashlib.md5()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            if stopped():
                raise InterruptedError()
            sha.update(chunk)
            md5.update(chunk)
    return sha.hexdigest(), md5.hexdigest()


def cloud_archive_scope(root, path, digest, targets):
    """Independent series bindings do not become each other's archive prerequisites."""
    if not (Path(root)/'drive_ingest.sqlite3').exists():
        return list(targets)
    origins = DriveJournal(root).origins(path, digest)
    if not origins:
        return list(targets)  # Existing local-source task semantics stay intact.
    folders = {entry['binding'].rsplit('|',1)[-1] for entry in origins}
    owners = {entry['target'] for entry in origins}
    return [t for t in targets if t.key in owners or not DriveBinding.read(t.account.drive_binding).enabled
        or DriveBinding.read(t.account.drive_binding).folder_id in folders]


class DriveIngestor:
    def __init__(self, root, client, stopped=lambda: False, log=lambda text: None,
                 status=lambda key, text: None, clock=None, ready=lambda path: None):
        self.root, self.client = Path(root), client
        self.journal = DriveJournal(root)
        self.stopped, self.log, self.status, self.ready = stopped, log, status, ready
        self.clock = clock or (lambda: datetime.now(CHINA_TIME))

    def validate(self, path, file):
        if path.stat().st_size != int(file['size']):
            raise DriveError('下载长度与云端大小不符，保留任务等待重试')
        digest, md5 = file_hashes(path, self.stopped)
        if not file.get('md5Checksum') or md5 != file['md5Checksum']:
            raise DriveError('下载摘要校验失败或云端尚未完成写入，等待下一轮')
        return digest

    def poll(self, target):
        binding = DriveBinding.read(target.account.drive_binding)
        if not binding.enabled or not target.account.enabled:
            return
        if not binding.folder_id or not target.account.target_dir:
            self.status(target.key, '请配置云盘目录与本地目录')
            return
        directory = Path(target.account.target_dir)
        if not directory.is_dir():
            self.status(target.key, '本地目录不可访问，未下载')
            return
        today = self.clock().astimezone(CHINA_TIME).date()
        day = self.journal.resume_day(target, today)
        self.journal.save_scan_day(target, day)
        advance = True
        while day <= today:
            if self.stopped():
                raise InterruptedError()
            completed = self.poll_day(target, binding, directory, day.isoformat(), day == today)
            if self.stopped():
                raise InterruptedError()
            advance = advance and completed
            # Do not pass an unfinished day, even if later dates can be processed.
            if advance:
                self.journal.save_scan_day(target, min(day + timedelta(days=1), today))
            day += timedelta(days=1)
        if not advance:
            pending_day = self.journal.resume_day(target,today).isoformat()
            self.status(target.key,f'补扫待重试：{pending_day} 起有未完成文件；已检查至 {today.isoformat()}')

    def poll_day(self, target, binding, directory, day, is_today):
        prefix = '今日' if is_today else '补扫'
        folder, files = self.client.today_files(binding.folder_id, day)
        if not folder:
            self.status(target.key, f'{prefix}：{day} 未找到'+('，等待创建' if is_today else '，跳过'))
            return True
        self.status(target.key, f'{prefix}：{day} 已找到')
        complete = True
        for file in files:
            if self.stopped():
                raise InterruptedError()
            if Path(file['name']).suffix.lower() not in selected_extensions(target.account):
                continue
            if file.get('mimeType', '').startswith('application/vnd.google-apps.'):
                continue  # No shortcuts/exports; binary media only.
            if not file.get('size') or int(file['size']) <= 0 or not file.get('md5Checksum'):
                complete = False
                continue  # Still being produced, or not a downloadable binary.
            if file.get('capabilities', {}).get('canDownload') is False:
                complete = False
                self.log(f'[云盘不可下载] {target.key} / {file["name"]}')
                continue
            entry = self.journal.reserve(target, file)
            path = Path(entry['path'])
            marker = path.with_name('.' + path.name + '.shusheng-pending')
            if entry['state'] == 'DOWNLOADED':
                if marker.exists():
                    marker.unlink(missing_ok=True)
                    if path.exists():
                        self.ready(str(path))
                continue  # Same version remains consumed across polls/restarts/archives.
            partial = directory / ('.shusheng-' + entry['identity'] + '.part')
            owns_final = marker.exists()
            try:
                digest = None
                if path.exists():
                    try:
                        digest = self.validate(path, file)
                    except DriveError:
                        # Someone replaced our reserved name; never overwrite it.
                        raise DriveError('下载目标已存在且内容不同，请移开同名文件后重试')
                else:
                    self.status(target.key, '正在下载：' + file['name'])
                    last_progress = [0.0]
                    def progress(size):
                        if time.monotonic() - last_progress[0] >= 1:
                            last_progress[0] = time.monotonic()
                            self.status(target.key, f'下载 {size * 100 / int(file["size"]):.0f}%：{file["name"]}')
                    self.client.download(file['id'], partial, self.stopped, progress)
                    digest = self.validate(partial, file)
                    latest = self.client.metadata(file['id'])
                    if version_of(latest) != version_of(file):
                        raise DriveError('下载期间云端文件发生变化，下一轮重新下载')
                    if self.stopped():
                        raise InterruptedError()
                    # Exclusive move: on Windows rename refuses an existing destination.
                    # The single ingestion worker owns reservations; no replacement of user files.
                    if path.exists():
                        raise DriveError('本地出现同名文件，未覆盖，请检查后重试')
                    marker.touch(exist_ok=True)
                    partial.rename(path)
                owns_final = True
                marker.touch(exist_ok=True)
                from .multi_deploy import MultiTargetLedger
                duplicate = MultiTargetLedger(self.root).result(digest, target.key).get('status') == 'SUCCESS'
                self.journal.complete(entry['identity'], digest, duplicate)
                # Give the existing fully-written check an old mtime only after journal commit.
                os.utime(path, (time.time() - 5, time.time() - 5))
                marker.unlink(missing_ok=True)
                self.log(f'[云盘下载完成] {target.key} / {path.name}；SHA-256已校验' + ('；历史成功重复，跳过上传' if duplicate else '；进入本地部署队列'))
                self.ready(str(path))
            except (InterruptedError, DriveAuthRequired):
                raise
            except (DriveError, OSError) as error:
                complete = False
                self.log(f'[云盘文件未就绪] {target.key} / {file["name"]}：{error}；保留任务，下轮重试')
            finally:
                partial.unlink(missing_ok=True)
                if not owns_final or not path.exists():
                    marker.unlink(missing_ok=True)
        self.status(target.key, f'{prefix}：{day} 已找到 · '+('检查完成' if complete else '部分文件待重试'))
        return complete


class DriveWatchWorker(QThread):
    log = Signal(str)
    binding_status = Signal(str, str)
    file_ready = Signal(str)
    authorization_error = Signal(str, int)

    def __init__(self, root, credentials, parent=None, client=None):
        super().__init__(parent)
        self.root, self.credentials = root, credentials
        self.client = client or DriveClient(credentials)
        self.stopping, self.wake = threading.Event(), threading.Event()
        self.lock = threading.Lock()
        self.targets, self.next_poll = [], {}
        self.paused = True
        self.immediate = set()
        self.revision = 0
        self.configuration = None

    def configure(self, targets, paused=False):
        token = (paused, tuple(sorted((t.key, t.account.target_dir, t.account.enabled,
            tuple(t.account.media_types), json.dumps(t.account.drive_binding,sort_keys=True)) for t in targets)))
        with self.lock:
            if token == self.configuration:
                return
            self.configuration = token
            self.targets = [replace(t, account=replace(t.account, drive_binding=dict(t.account.drive_binding))) for t in targets]
            self.paused = paused
            self.revision += 1
        self.wake.set()

    def check_now(self, key):
        with self.lock:
            self.immediate.add(key)
        self.wake.set()

    def stop(self):
        self.stopping.set()
        self.wake.set()

    def run(self):
        try:
            self.watch_loop()
        except Exception as error:
            self.log.emit('[云盘监听停止] '+str(error)+'；本地发布不受影响，请检查存储后重新启动书声')

    def watch_loop(self):
        ingestor = DriveIngestor(self.root, self.client, log=self.log.emit,
            status=self.binding_status.emit, ready=self.file_ready.emit)
        while not self.stopping.is_set():
            with self.lock:
                targets, paused, revision = list(self.targets), self.paused, self.revision
                immediate = self.immediate.copy()
                self.immediate.clear()
            if self.credentials.connected():
                for target in targets:
                    binding = DriveBinding.read(target.account.drive_binding)
                    signature = (target.key, binding.folder_id, target.account.target_dir, tuple(target.account.media_types),binding.poll_seconds)
                    if not binding.enabled or (paused and target.key not in immediate):
                        continue
                    if target.key not in immediate and time.monotonic() < self.next_poll.get(signature, 0):
                        continue
                    def cancelled():
                        with self.lock:
                            return self.stopping.is_set() or revision != self.revision or not self.credentials.connected()
                    ingestor.stopped = cancelled
                    generation = self.credentials.generation
                    try:
                        ingestor.poll(target)
                    except InterruptedError:
                        break
                    except DriveAuthRequired as error:
                        self.authorization_error.emit(str(error), generation)
                        self.binding_status.emit(target.key, '授权失效，请重新连接')
                        self.next_poll[signature] = time.monotonic() + 60
                    except Exception as error:
                        self.binding_status.emit(target.key, '检查失败：' + str(error))
                        self.log.emit(f'[云盘检查失败] {target.key}：{error}；其他绑定继续，云端不修改')
                    self.next_poll[signature] = time.monotonic() + binding.poll_seconds
            self.wake.wait(.5)
            self.wake.clear()
