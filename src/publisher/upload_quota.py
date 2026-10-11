"""Durable, transactional daily upload allowance per platform/account."""
import sqlite3
import json
import hashlib
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path

CHINA_TIME = timezone(timedelta(hours=8))


class DailyQuotaReached(RuntimeError):
    pass


class DailyUploadQuota:
    def __init__(self, root, clock=None):
        self.path = Path(root) / 'upload_quota.sqlite3'
        self.clock = clock or (lambda: datetime.now(CHINA_TIME))

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute('CREATE TABLE IF NOT EXISTS limits (target TEXT PRIMARY KEY, maximum INTEGER NOT NULL CHECK(maximum>=0))')
        connection.execute('CREATE TABLE IF NOT EXISTS uploads (id TEXT PRIMARY KEY, day TEXT NOT NULL, target TEXT NOT NULL, digest TEXT NOT NULL, task TEXT NOT NULL, started_at TEXT NOT NULL)')
        connection.execute('CREATE INDEX IF NOT EXISTS uploads_day ON uploads(day,target)')
        connection.execute('CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        connection.commit()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def day(self):
        return self.clock().astimezone(CHINA_TIME).date().isoformat()

    def set_limit(self, target, maximum):
        if type(maximum) is not int or maximum < 0:
            raise ValueError('每日上传上限必须为非负整数，0表示暂停该账号')
        with self.connect() as db:
            db.execute('INSERT INTO limits VALUES (?,?) ON CONFLICT(target) DO UPDATE SET maximum=excluded.maximum', (target, maximum))

    def bootstrap_history(self,stop_requested=lambda:False):
        """Import provable real attempts once, without charging historical dedup skips."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM metadata WHERE key='history_imported'").fetchone():
                return
            for path in sorted((self.path.parent/'publish_reports').glob('*.json')):
                if stop_requested():
                    raise InterruptedError('计数迁移已取消')
                report=json.loads(path.read_text(encoding='utf-8-sig'))
                task=report.get('task_id',path.name)
                for item in report.get('files',[]):
                    for target,result in item.get('results',{}).items():
                        for number,attempt in enumerate(result.get('attempts',[]),1):
                            logs='\n'.join(entry.get('message','') for entry in attempt.get('logs',[]))
                            started=(attempt.get('upload_started') is True or attempt.get('status')=='SUCCESS'
                                or any(label in logs for label in ('[选择视频]','视频文件已选择','[上传完成]')))
                            timestamp=attempt.get('upload_started_at') or attempt.get('started_at')
                            if not started or not timestamp:
                                continue
                            stamp=datetime.fromisoformat(timestamp)
                            if stamp.tzinfo is None:
                                stamp=stamp.replace(tzinfo=CHINA_TIME)
                            stamp=stamp.astimezone(CHINA_TIME)
                            digest=item.get('sha256','')
                            identifier=attempt.get('id') or hashlib.sha256(f'{task}:{target}:{digest}:{number}'.encode()).hexdigest()
                            db.execute('INSERT OR IGNORE INTO uploads VALUES (?,?,?,?,?,?)',
                                (identifier,stamp.date().isoformat(),target,digest,task,stamp.isoformat()))
            db.execute("INSERT INTO metadata VALUES ('history_imported','1')")

    def snapshot(self, target, default=10):
        day = self.day()
        if not self.path.exists():
            return {'day': day, 'limit': default, 'used': 0, 'remaining': default}
        with self.connect() as db:
            limit = db.execute('SELECT maximum FROM limits WHERE target=?', (target,)).fetchone()
            used = db.execute('SELECT COUNT(*) FROM uploads WHERE day=? AND target=?', (day, target)).fetchone()[0]
        maximum = limit[0] if limit else default
        return {'day': day, 'limit': maximum, 'used': used, 'remaining': max(0, maximum-used)}

    def reserve(self, target, digest, task, attempt_id, default=10):
        """Call immediately before setting the browser file input. Never refund an uncertain transfer."""
        stamp = self.clock().astimezone(CHINA_TIME)
        day = stamp.date().isoformat()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing=db.execute('SELECT started_at FROM uploads WHERE id=?', (attempt_id,)).fetchone()
            if existing:
                return existing[0]
            limit = db.execute('SELECT maximum FROM limits WHERE target=?', (target,)).fetchone()
            maximum = limit[0] if limit else default
            used = db.execute('SELECT COUNT(*) FROM uploads WHERE day=? AND target=?', (day, target)).fetchone()[0]
            if used >= maximum:
                raise DailyQuotaReached(f'今日已上传{used}次，上限{maximum}次；北京时间次日00:00或提高本账号上限后继续')
            db.execute('INSERT INTO uploads VALUES (?,?,?,?,?,?)',
                       (attempt_id, day, target, digest, task, stamp.isoformat()))
            return stamp.isoformat()
