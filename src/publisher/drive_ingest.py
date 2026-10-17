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


def canonical_local_path(path):
    """Windows receiving-directory identity; preserve actual file names on disk."""
    return os.path.normcase(os.path.normpath(str(Path(path).resolve())))


def journal_identity(target_key, binding, cloud_id, version):
    return hashlib.sha256(json.dumps([target_key, binding, cloud_id, version]).encode()).hexdigest()


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
            db.execute('CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)')

    def pin_legacy_account(self, cloud_account):
        """Freeze the verified pre-upgrade owner before any future reconnection."""
        if not cloud_account:
            raise DriveError('无法确认原云盘账号')
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('legacy_cloud_account',?)",(cloud_account,))

    def prepare_account(self, cloud_account):
        """Attribute pre-upgrade history once, then keep that owner across switches."""
        if not cloud_account:
            raise DriveError('无法确认云盘账号，未使用下载历史')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM metadata WHERE key='account_scope_v2'").fetchone():
                return
            # Prefer the owner pinned at upgrade; otherwise the first verified
            # connection establishes it. A later account can never claim it.
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('legacy_cloud_account',?)", (cloud_account,))
            legacy_owner = db.execute("SELECT value FROM metadata WHERE key='legacy_cloud_account'").fetchone()[0]
            db.row_factory = sqlite3.Row
            for row in db.execute('SELECT * FROM files').fetchall():
                parts = row['binding'].split('|')
                if len(parts) != 2:
                    continue
                binding = '|'.join((canonical_local_path(parts[0]), legacy_owner, parts[1]))
                identity = journal_identity(row['target'],binding,row['cloud_id'],row['version'])
                db.execute('''INSERT INTO files VALUES (?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(identity) DO UPDATE SET path=excluded.path,state=excluded.state,
                    sha256=excluded.sha256,duplicate=excluded.duplicate
                    WHERE files.state!='DOWNLOADED' AND excluded.state='DOWNLOADED' ''',
                    (identity,row['target'],binding,row['cloud_id'],row['version'],row['path'],
                     row['state'],row['sha256'],row['duplicate']))
                db.execute('DELETE FROM files WHERE identity=?',(row['identity'],))
            for row in db.execute('SELECT * FROM scan_cursor').fetchall():
                parts = row['binding'].split('|')
                if len(parts) != 2:
                    continue
                binding = '|'.join((canonical_local_path(parts[0]), legacy_owner, parts[1]))
                db.execute('''INSERT INTO scan_cursor VALUES (?,?,?) ON CONFLICT(target,binding)
                    DO UPDATE SET day=min(day,excluded.day)''',(row['target'],binding,row['day']))
                db.execute('DELETE FROM scan_cursor WHERE target=? AND binding=?',(row['target'],row['binding']))
            db.execute("INSERT INTO metadata VALUES ('account_scope_v2','1')")

    def resume_day(self, target, today, cloud_account=''):
        binding = self.identity(target, {'id': ''}, cloud_account)[1]
        with self.connect() as db:
            row = db.execute('SELECT day FROM scan_cursor WHERE target=? AND binding=?',
                (target.key,binding)).fetchone()
        return min(date.fromisoformat(row[0]), today) if row else today

    def save_scan_day(self, target, day, cloud_account=''):
        binding = self.identity(target, {'id': ''}, cloud_account)[1]
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
    def identity(target, file, cloud_account=''):
        parts = [canonical_local_path(target.account.target_dir)]
        if cloud_account:
            parts.append(cloud_account)
        parts.append(DriveBinding.read(target.account.drive_binding).folder_id)
        binding = '|'.join(parts)
        return journal_identity(target.key,binding,file['id'],version_of(file)), binding

    def entry(self, identity):
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute('SELECT * FROM files WHERE identity=?', (identity,)).fetchone()
            return dict(row) if row else None

    def shared_source(self, binding, file, *, exclude=None, include_missing=False):
        """Share bytes only for the exact cloud version and receiving directory/series."""
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT * FROM files WHERE binding=? AND cloud_id=? AND version=? AND state='DOWNLOADED' ORDER BY rowid",
                (binding,file['id'],version_of(file))).fetchall()
        return next((dict(row) for row in rows if row['identity'] != exclude and
            (include_missing or Path(row['path']).is_file())),None)

    def reserve(self, target, file, cloud_account=''):
        identity, binding = self.identity(target, file, cloud_account)
        existing = self.entry(identity)
        if existing:
            if existing['state'] == 'FETCHING' and not Path(existing['path']).exists():
                shared = self.shared_source(binding,file,exclude=identity)
                if shared:
                    with self.connect() as db:
                        db.execute('UPDATE files SET path=? WHERE identity=?',(shared['path'],identity))
                    return self.entry(identity)
            return existing
        directory = Path(target.account.target_dir).resolve()
        path = directory / safe_name(file['name'])
        shared = self.shared_source(binding,file)
        if shared:
            path = Path(shared['path'])
        else:
            with self.connect() as db:
                claimed = any(canonical_local_path(p) == canonical_local_path(path)
                    for (p,) in db.execute('SELECT path FROM files WHERE binding!=?',(binding,)))
            if path.exists() or claimed:
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
            return [dict(r) for r in db.execute("SELECT * FROM files WHERE sha256=? AND state='DOWNLOADED'",
                (digest,)) if canonical_local_path(r['path']) == canonical_local_path(path)]

    def mark_duplicate(self, target, path, digest):
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT identity,path FROM files WHERE target=? AND sha256=? AND state='DOWNLOADED'",
                (target,digest)).fetchall()
            db.executemany('UPDATE files SET duplicate=1 WHERE identity=?',
                [(r['identity'],) for r in rows if canonical_local_path(r['path']) == canonical_local_path(path)])


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
        self.cloud_account = ''

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
        if self.stopped():
            raise InterruptedError()
        self.cloud_account = self.client.account_identity()
        if self.stopped():
            raise InterruptedError()
        self.journal.prepare_account(self.cloud_account)
        today = self.clock().astimezone(CHINA_TIME).date()
        day = self.journal.resume_day(target, today, self.cloud_account)
        self.journal.save_scan_day(target, day, self.cloud_account)
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
                self.journal.save_scan_day(target, min(day + timedelta(days=1), today), self.cloud_account)
            day += timedelta(days=1)
        if not advance:
            pending_day = self.journal.resume_day(target,today,self.cloud_account).isoformat()
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
            entry = self.journal.reserve(target, file, self.cloud_account)
            path = Path(entry['path'])
            marker = path.with_name('.' + path.name + '.shusheng-pending')
            if entry['state'] == 'DOWNLOADED':
                if marker.exists():
                    marker.unlink(missing_ok=True)
                    if path.exists():
                        self.ready(str(path))
                continue  # Same version remains consumed across polls/restarts/archives.
            identity, scope = self.journal.identity(target,file,self.cloud_account)
            completed_source = self.journal.shared_source(scope,file,exclude=identity,include_missing=True)
            if completed_source and not Path(completed_source['path']).exists():
                from .multi_deploy import MultiTargetLedger
                digest = completed_source['sha256']
                if MultiTargetLedger(self.root).result(digest,target.key).get('status') == 'SUCCESS':
                    # The shared file can be published/archived before this
                    # account's next poll. Preserve consumption without fetching it again.
                    self.journal.complete(entry['identity'],digest,False)
                    continue
            shared = self.journal.shared_source(entry['binding'],file,exclude=entry['identity'])
            if shared and Path(shared['path']) == path:
                try:
                    digest = self.validate(path,file)
                    from .multi_deploy import MultiTargetLedger
                    duplicate = MultiTargetLedger(self.root).result(digest,target.key).get('status') == 'SUCCESS'
                    self.journal.complete(entry['identity'],digest,duplicate)
                    # Other accounts may already be uploading this path. Do not
                    # change its mtime or add a pending marker when sharing it.
                    self.log(f'[云盘共用文件] {target.key} / {path.name}；同目录同版本无需重复下载')
                    self.ready(str(path))
                except (DriveError,OSError) as error:
                    complete = False
                    self.log(f'[云盘共用文件未就绪] {target.key} / {path.name}：{error}；下轮重试')
                continue
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
                    generation = self.credentials.generation
                    def cancelled():
                        with self.lock:
                            return (self.stopping.is_set() or revision != self.revision
                                or generation != self.credentials.generation or not self.credentials.connected())
                    ingestor.stopped = cancelled
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
