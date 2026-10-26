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
    def __init__(self, root, stopped=lambda: False):
        self.stopped = stopped
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
            db.execute('''CREATE TABLE IF NOT EXISTS file_provenance (
                identity TEXT PRIMARY KEY, source_day TEXT NOT NULL DEFAULT '',
                downloaded_at TEXT NOT NULL DEFAULT '', size_bytes INTEGER, mtime_ns INTEGER)''')
            db.execute('''CREATE TABLE IF NOT EXISTS manual_holds (
                hold_id TEXT PRIMARY KEY,binding TEXT NOT NULL,cloud_id TEXT NOT NULL,sha256 TEXT NOT NULL,
                md5 TEXT NOT NULL,size_bytes INTEGER NOT NULL,original_path TEXT NOT NULL,held_path TEXT NOT NULL,
                source_day TEXT NOT NULL DEFAULT '',target_keys TEXT NOT NULL,created_at TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,released_at TEXT NOT NULL DEFAULT '')''')
            db.execute('CREATE INDEX IF NOT EXISTS files_scope_state ON files(binding,state,cloud_id)')
            db.execute('CREATE INDEX IF NOT EXISTS manual_hold_content ON manual_holds(binding,md5,size_bytes,active)')

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
                db.execute('UPDATE OR IGNORE file_provenance SET identity=? WHERE identity=?',
                           (identity,row['identity']))
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
        """Reuse owned bytes in the same scope, even after metadata-only changes.

        A version counter is not a content identity. Existing bytes must match
        the requested size/MD5 and their recorded SHA-256 before sharing.
        Missing archived bytes can only be consumed by an exact-version match.
        """
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("""SELECT * FROM files WHERE binding=? AND cloud_id=?
                AND state IN ('DOWNLOADED','FETCHING') ORDER BY (version=?) DESC,rowid""",
                (binding,file['id'],version_of(file))).fetchall()
        for row in rows:
            if self.stopped():raise InterruptedError()
            if row['identity'] == exclude:continue
            path=Path(row['path'])
            try:
                if not path.is_file():
                    if include_missing and row['state']=='DOWNLOADED' and row['version']==version_of(file):
                        return dict(row)
                    continue
                before=path.stat()
                if before.st_size != int(file['size']) or not file.get('md5Checksum'):continue
                digest,md5=file_hashes(path,self.stopped)
                after=path.stat()
                if ((before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns)
                        or md5.lower()!=file['md5Checksum'].lower()
                        or row['sha256'] and row['sha256']!=digest):continue
                return dict(row,sha256=digest)
            except InterruptedError:
                raise
            except OSError:
                continue
        return None

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
            db.execute('''INSERT INTO file_provenance(identity,downloaded_at) VALUES (?,?)
                ON CONFLICT(identity) DO UPDATE SET downloaded_at=CASE
                WHEN downloaded_at='' THEN excluded.downloaded_at ELSE downloaded_at END''',
                (identity,datetime.now(CHINA_TIME).isoformat()))
        self.record_local_stamp(identity)

    def record_source_day(self, identity, day):
        day = date.fromisoformat(day).isoformat()
        with self.connect() as db:
            db.execute('''INSERT INTO file_provenance(identity,source_day) VALUES (?,?)
                ON CONFLICT(identity) DO UPDATE SET source_day=CASE
                WHEN source_day='' THEN excluded.source_day ELSE source_day END''',(identity,day))

    def record_local_stamp(self, identity):
        entry = self.entry(identity)
        try:
            stamp = Path(entry['path']).stat()
        except OSError:
            return
        with self.connect() as db:
            db.execute('UPDATE file_provenance SET size_bytes=?,mtime_ns=? WHERE identity=?',
                       (stamp.st_size,stamp.st_mtime_ns,identity))

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


def queue_provenance(root, files):
    """Read provenance in bulk without hashing videos or opening a write journal."""
    journal = Path(root)/'drive_ingest.sqlite3'
    if not journal.is_file():
        return {}
    wanted = {canonical_local_path(path):stamp for path,stamp in files}
    found = {}
    try:
        with sqlite3.connect(journal.resolve().as_uri()+'?mode=ro',uri=True,timeout=.2) as db:
            db.row_factory=sqlite3.Row
            columns = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            query = ('''SELECT f.path,p.source_day,p.downloaded_at,p.size_bytes,p.mtime_ns
                FROM files f LEFT JOIN file_provenance p ON f.identity=p.identity
                WHERE f.state='DOWNLOADED' ''' if 'file_provenance' in columns else
                "SELECT path,NULL source_day,NULL downloaded_at,NULL size_bytes,NULL mtime_ns FROM files WHERE state='DOWNLOADED'")
            for row in db.execute(query):
                path=canonical_local_path(row['path'])
                stamp=wanted.get(path)
                if stamp is None or (row['size_bytes'] is not None and
                        (row['size_bytes'],row['mtime_ns']) != (stamp.st_size,stamp.st_mtime_ns)):
                    continue
                item=found.setdefault(path,{'days':set(),'downloaded_at':''})
                if row['source_day']:
                    item['days'].add(row['source_day'])
                if row['downloaded_at'] and (not item['downloaded_at'] or row['downloaded_at']<item['downloaded_at']):
                    item['downloaded_at']=row['downloaded_at']
    except sqlite3.Error:
        return {path:{'label':'日期未知','tooltip':'云盘来源日期暂不可读取'} for path in wanted}
    result={}
    for path,item in found.items():
        days=sorted(item['days'])
        result[path]={'label':days[0] if len(days)==1 else '多日期' if days else '日期未知',
                      'tooltip':'云盘来源日期：'+('、'.join(days) or '历史记录未保存日期')
                      +'\n下载完成时间：'+(item['downloaded_at'] or '历史记录未保存时间')}
    return result


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
                 status=lambda key, text: None, clock=None, ready=lambda path: None, storage_policy=None):
        self.root, self.client = Path(root), client
        self.journal = DriveJournal(root,lambda:self.stopped())
        self.stopped, self.log, self.status, self.ready = stopped, log, status, ready
        self.clock = clock or (lambda: datetime.now(CHINA_TIME))
        self.cloud_account = ''
        self.storage_policy = storage_policy

    def validate(self, path, file):
        if path.stat().st_size != int(file['size']):
            raise DriveError('下载长度与云端大小不符，保留任务等待重试')
        digest, md5 = file_hashes(path, self.stopped)
        if not file.get('md5Checksum') or md5 != file['md5Checksum']:
            raise DriveError('下载摘要校验失败或云端尚未完成写入，等待下一轮')
        return digest

    def poll(self, target):
        self.last_scans=[]
        self.cloud_account=''
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
        from .manual_hold import discover_manual_holds
        discover_manual_holds(self.root,[target],stopped=self.stopped,log=self.log,cloud_account=self.cloud_account)
        today = self.clock().astimezone(CHINA_TIME).date()
        day = self.journal.resume_day(target, today, self.cloud_account)
        self.journal.save_scan_day(target, day, self.cloud_account)
        self.log(f'[云盘扫描开始] {target.key}；Google云盘目录={binding.folder_path or binding.folder_id}；'
            f'从北京时间日期断点 {day.isoformat()} 扫描到 {today.isoformat()}')
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
        return advance

    def poll_day(self, target, binding, directory, day, is_today):
        prefix = '今日' if is_today else '补扫'
        folder, files = self.client.today_files(binding.folder_id, day)
        listing={'day':day,'folder_found':bool(folder),'folder_id':folder.get('id') if folder else None,
                 'entries_seen':len(files),'media_files':[],'pending_files':[]}
        self.last_scans=[*getattr(self,'last_scans',[]),listing][-2:]
        self.log(f'[云盘连接已核验] {target.key} / {day}；目录列表请求成功；读取{len(files)}个条目')
        if not folder:
            self.status(target.key, f'{prefix}：{day} 未找到'+('，等待创建' if is_today else '，跳过'))
            return True
        if len(folder.get('existing_folder_ids',[]))>1:
            self.log(f'[日期目录容错] {target.key} / {day}；找到{len(folder["existing_folder_ids"])}个同名目录，'
                     '遍历全部已有目录，按文件ID合并，不新建或删除云端目录')
        self.status(target.key, f'{prefix}：{day} 已找到')
        complete = True
        for file in files:
            if self.stopped():
                raise InterruptedError()
            if Path(file['name']).suffix.lower() not in selected_extensions(target.account):
                continue
            if file.get('mimeType', '').startswith('application/vnd.google-apps.'):
                continue  # No shortcuts/exports; binary media only.
            file_state={'name':file['name'],'size':file.get('size'),'md5_ready':bool(file.get('md5Checksum')),
                        'can_download':file.get('capabilities',{}).get('canDownload',True)}
            if len(listing['media_files'])<30:
                listing['media_files'].append(file_state)
            if not file.get('size') or int(file['size']) <= 0 or not file.get('md5Checksum'):
                complete = False
                reason='云端尚未提供完整文件大小或MD5，等待文件就绪后重试'
                if len(listing['pending_files'])<30:
                    listing['pending_files'].append({'name':file['name'],'reason':reason})
                self.log(f'[云盘文件未就绪] {target.key} / {file["name"]}；{reason}')
                continue  # Still being produced, or not a downloadable binary.
            if file.get('capabilities', {}).get('canDownload') is False:
                complete = False
                if len(listing['pending_files'])<30:
                    listing['pending_files'].append({'name':file['name'],'reason':'云盘返回不可下载权限'})
                self.log(f'[云盘不可下载] {target.key} / {file["name"]}')
                continue
            from .manual_hold import find_cloud_manual_hold
            hold=find_cloud_manual_hold(self.root,target,self.cloud_account,file)
            if hold:
                file_state.update(journal_state='MANUAL_HOLD',hold_id=hold['hold_id'],held_path=hold['held_path'])
                self.log(f'[云盘人工暂停] {target.key} / {file["name"]}；隔离编号={hold["hold_id"]}；'
                         '同云盘账号、目录、本地目录及内容，不下载、不计发布成功；请从恢复人工暂停入口明确恢复')
                continue
            entry = self.journal.reserve(target, file, self.cloud_account)
            self.journal.record_source_day(entry['identity'], day)
            path = Path(entry['path'])
            marker = path.with_name('.' + path.name + '.shusheng-pending')
            if entry['state'] == 'DOWNLOADED':
                file_state.update(journal_state='DOWNLOADED',local_exists=path.is_file())
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
                    marker.unlink(missing_ok=True)  # A verified final file can survive a prior crash.
                    # Other accounts may already be uploading this path. Do not
                    # change its mtime or add a pending marker when sharing it.
                    self.log(f'[云盘共用文件] {target.key} / {path.name}；同本地目录、云盘账号及云盘路径；'
                             '云端文件ID、大小和内容摘要一致，版本信息变化也不重复下载')
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
                    if self.storage_policy:
                        self.storage_policy.check(directory, int(file['size']))
                    self.log(f'[开始下载] {target.key} / {file["name"]}；云端文件ID={file["id"]}；'
                        f'大小={file["size"]}字节；保存到={path}；云盘日期={day}')
                    self.status(target.key, f'正在下载：{day} / '+file['name'])
                    last_progress = [0.0]
                    def progress(size):
                        if size > int(file['size']):
                            raise DriveError('下载超过云端声明大小，停止并等待重新校验')
                        if time.monotonic() - last_progress[0] >= 1:
                            if self.storage_policy:
                                self.storage_policy.check_progress(directory)
                            last_progress[0] = time.monotonic()
                            self.status(target.key, f'{day} 下载 {size * 100 / int(file["size"]):.0f}%：{file["name"]}')
                    self.client.download(file['id'], partial, self.stopped, progress)
                    digest = self.validate(partial, file)
                    latest = self.client.metadata(file['id'])
                    if (latest.get('id')!=file['id'] or str(latest.get('size'))!=str(file['size'])
                            or latest.get('md5Checksum','').lower()!=file['md5Checksum'].lower()):
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
                self.journal.record_local_stamp(entry['identity'])
                marker.unlink(missing_ok=True)
                self.log(f'[云盘下载完成] {target.key} / {path.name}；云盘日期={day}；SHA-256已校验' + ('；历史成功重复，跳过上传' if duplicate else '；进入本地部署队列'))
                self.ready(str(path))
            except (InterruptedError, DriveAuthRequired):
                raise
            except (DriveError, OSError) as error:
                complete = False
                if len(listing['pending_files'])<30:
                    listing['pending_files'].append({'name':file['name'],'reason':str(error)})
                self.log(f'[云盘文件未就绪] {target.key} / {file["name"]}：{error}；保留任务，下轮重试')
            finally:
                partial.unlink(missing_ok=True)
                if not owns_final or not path.exists():
                    marker.unlink(missing_ok=True)
        listing['complete']=complete
        self.status(target.key, f'{prefix}：{day} 已找到 · '+('检查完成' if complete else '部分文件待重试'))
        return complete


class DriveWatchWorker(QThread):
    log = Signal(str)
    event_logged = Signal(dict)
    binding_status = Signal(str, str)
    file_ready = Signal(str)
    authorization_error = Signal(str, int)

    def __init__(self, root, credentials, parent=None, client=None, clock=None):
        super().__init__(parent)
        self.root, self.credentials = root, credentials
        from .event_log import event_log
        self.events=event_log(root)
        self.client = client or DriveClient(credentials)
        self.stopping, self.wake = threading.Event(), threading.Event()
        self.lock = threading.Lock()
        self.targets, self.next_poll = [], {}
        self.paused = True
        self.immediate = set()
        self.revision = 0
        self.configuration = None
        self.storage_policy = None
        self.failures = {}
        self.poll_seconds = 15
        self.clock=clock or (lambda:datetime.now(CHINA_TIME))
        self.pause_reason='等待启动'
        self.scan_history=[]
        self.active_scan=None
        self._cloud_scan=None
        self.cloud_account=''
        self.cloud_account_generation=None
        self.check_all_pending=False

    def emit_log(self,message):
        self.log.emit(message)
        target=getattr(self,'current_target',None)
        context=({'binding_platform':target.platform,'account_id':target.account.account_id,
            'account_name':target.account.account_name} if target else {})
        context.update(platform='common',category='cloud')
        context['task_id']=getattr(self,'task_id','')
        if self._cloud_scan:context['cloud_scan_id']=self._cloud_scan.scan_id
        event=self.events.write(message,**context)
        if self._cloud_scan:
            try:self._cloud_scan.record(event)
            except OSError:pass  # The ordinary cloud log still remains available.
        self.event_logged.emit(event)

    def configure(self, targets, paused=False, poll_seconds=15, pause_reason=''):
        DriveBinding.read({'poll_seconds':poll_seconds})
        token = (paused, poll_seconds, pause_reason, tuple(sorted((t.key, t.account.target_dir, t.account.enabled,
            tuple(t.account.media_types), json.dumps(t.account.drive_binding,sort_keys=True)) for t in targets)))
        with self.lock:
            if token == self.configuration:
                return
            self.configuration = token
            self.targets = [replace(t, account=replace(t.account, drive_binding=dict(t.account.drive_binding))) for t in targets]
            keys={t.key for t in self.targets}
            self.next_poll={key:due for key,due in self.next_poll.items() if key[0] in keys}
            self.immediate.intersection_update(keys)
            if not keys:self.check_all_pending=False
            self.paused = paused
            self.poll_seconds = poll_seconds
            self.pause_reason=pause_reason or ('监听已暂停' if paused else '')
            self.revision += 1
        self.wake.set()

    def check_now(self, key):
        with self.lock:
            self.immediate.add(key)
        self.wake.set()

    def check_all_now(self):
        """Queue one shared request; never interrupt or parallelize an active round."""
        with self.lock:
            if self.paused or not self.targets or self.check_all_pending:return False
            self.check_all_pending=True
        self.wake.set()
        return True

    def stop(self):
        self.stopping.set()
        self.wake.set()

    def snapshot(self):
        with self.lock:
            today=self.clock().astimezone(CHINA_TIME).date().isoformat()
            return {'running':self.isRunning(),'paused':self.paused,'pause_reason':self.pause_reason,
                'current_day':today,
                'poll_seconds':self.poll_seconds,'target_keys':[t.key for t in self.targets],
                'active_scan':dict(self.active_scan) if self.active_scan else None,
                'cloud_account':self.cloud_account if self.cloud_account_generation==self.credentials.generation else '',
                'check_all_pending':self.check_all_pending,
                'cloud_scan_id':self._cloud_scan.scan_id if self._cloud_scan else '',
                'last_scans':list(self.scan_history),
                'next_checks':[{'key':key[0],'day':key[-1],'in_seconds':max(0,round(due-time.monotonic(),1))}
                    for key,due in self.next_poll.items() if key[-1]==today]}

    def run(self):
        try:
            self.watch_loop()
        except Exception as error:
            self.emit_log('[云盘监听停止] '+str(error)+'；本地发布不受影响，请检查存储后重新启动书声')
        finally:
            if self._cloud_scan:
                try:self._cloud_scan.finish('STOPPED' if self.stopping.is_set() else 'FAILED')
                except OSError:pass
                self._cloud_scan=None

    def watch_loop(self):
        ingestor = DriveIngestor(self.root, self.client, log=self.emit_log,
            status=self.binding_status.emit, ready=self.file_ready.emit, storage_policy=self.storage_policy,clock=self.clock)
        while not self.stopping.is_set():
            with self.lock:
                targets, paused, revision = list(self.targets), self.paused, self.revision
                poll_seconds = self.poll_seconds
                immediate = self.immediate.copy()
                self.immediate.clear()
                manual_all=self.check_all_pending
                self.check_all_pending=False
                if manual_all:immediate.update(t.key for t in targets)
            if self.credentials.connected():
                today=self.clock().astimezone(CHINA_TIME).date().isoformat()
                for target in targets:
                    self.current_target=target
                    binding = DriveBinding.read(target.account.drive_binding)
                    signature = (target.key, binding.folder_id, target.account.target_dir, tuple(target.account.media_types),poll_seconds,today)
                    if not binding.enabled or (paused and target.key not in immediate):
                        continue
                    if target.key not in immediate and time.monotonic() < self.next_poll.get(signature, 0):
                        continue
                    if self._cloud_scan is None:
                        from .cloud_diagnostics import CloudScanDiagnostics
                        self._cloud_scan=CloudScanDiagnostics(self.root,[t.key for t in targets],
                            'manual_all' if manual_all else 'manual_binding' if immediate else 'scheduled')
                    generation = self.credentials.generation
                    def cancelled():
                        with self.lock:
                            return (self.stopping.is_set() or revision != self.revision
                                or generation != self.credentials.generation or not self.credentials.connected())
                    ingestor.stopped = cancelled
                    scan={'target_key':target.key,'day':today,'started_at':self.clock().isoformat(),
                          'scan_id':self._cloud_scan.scan_id}
                    with self.lock:
                        self.active_scan=scan
                    try:
                        completed = ingestor.poll(target)
                        scan.update(status='COMPLETE' if completed is not False else 'PENDING',
                                    listing=list(getattr(ingestor,'last_scans',[])))
                        if completed is False:
                            self.failures[signature] = self.failures.get(signature, 0) + 1
                        else:
                            self.failures.pop(signature, None)
                    except InterruptedError:
                        scan.update(status='CANCELLED',message='监听配置变化、暂停或用户停止，本轮检查已取消')
                        break
                    except DriveAuthRequired as error:
                        scan.update(status='AUTH_REQUIRED',message=str(error))
                        self.authorization_error.emit(str(error), generation)
                        self.binding_status.emit(target.key, '授权失效，请重新连接')
                        self.failures[signature] = self.failures.get(signature, 0) + 1
                    except Exception as error:
                        scan.update(status='FAILED',message=str(error))
                        self.failures[signature] = self.failures.get(signature, 0) + 1
                        self.binding_status.emit(target.key, '检查失败：' + str(error))
                        self.emit_log(f'[云盘检查失败] {target.key}：{error}；其他绑定继续，云端不修改')
                    finally:
                        scan['finished_at']=self.clock().isoformat()
                        if ingestor.cloud_account:
                            self.cloud_account=ingestor.cloud_account
                            self.cloud_account_generation=generation
                        self._cloud_scan.results.append(dict(scan))
                        with self.lock:
                            self.active_scan=None
                            other=[s for s in self.scan_history if s['target_key']!=target.key]
                            recent=[s for s in self.scan_history if s['target_key']==target.key][-1:]
                            self.scan_history=[*other,*recent,dict(scan)][-128:]
                    failures = self.failures.get(signature, 0)
                    delay = max(poll_seconds,min(300,poll_seconds*2**min(failures-1,5))) if failures else poll_seconds
                    with self.lock:
                        self.next_poll={key:due for key,due in self.next_poll.items() if key[-1]==today}
                        self.failures={key:value for key,value in self.failures.items() if key[-1]==today}
                        self.next_poll[signature] = time.monotonic() + delay
            if self._cloud_scan:
                self._cloud_scan.finish('STOPPED' if self.stopping.is_set() else 'FINISHED')
                self._cloud_scan=None
            self.wake.wait(.5)
            self.wake.clear()
