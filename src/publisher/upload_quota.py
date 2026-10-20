"""Durable daily confirmed-publication allowance per platform/account."""
import sqlite3
import json
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
    def connect(self, stop_requested=lambda:False):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute('CREATE TABLE IF NOT EXISTS limits (target TEXT PRIMARY KEY, maximum INTEGER NOT NULL CHECK(maximum>=0))')
        connection.execute('CREATE TABLE IF NOT EXISTS uploads (id TEXT PRIMARY KEY, day TEXT NOT NULL, target TEXT NOT NULL, digest TEXT NOT NULL, task TEXT NOT NULL, started_at TEXT NOT NULL)')
        connection.execute('CREATE INDEX IF NOT EXISTS uploads_day ON uploads(day,target)')
        connection.execute('CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        connection.execute('''CREATE TABLE IF NOT EXISTS published (
            target TEXT NOT NULL, digest TEXT NOT NULL, day TEXT NOT NULL,
            task TEXT NOT NULL, confirmed_at TEXT NOT NULL, PRIMARY KEY(target,digest))''')
        connection.execute('CREATE INDEX IF NOT EXISTS published_day ON published(day,target)')
        connection.commit()
        try:
            self._import_success_history(connection, stop_requested)
            with connection:
                yield connection
        finally:
            connection.close()

    def day(self):
        return self.clock().astimezone(CHINA_TIME).date().isoformat()

    def set_limit(self, target, maximum):
        if type(maximum) is not int or maximum < 0:
            raise ValueError('每日成功发布上限必须为非负整数，0表示暂停该账号')
        with self.connect() as db:
            db.execute('INSERT INTO limits VALUES (?,?) ON CONFLICT(target) DO UPDATE SET maximum=excluded.maximum', (target, maximum))

    def bootstrap_history(self,stop_requested=lambda:False):
        """Migrate confirmed successes; retain the old transfer audit untouched."""
        with self.connect(stop_requested):
            pass

    def _stamp(self, value=None):
        stamp = datetime.fromisoformat(value) if isinstance(value,str) else value or self.clock()
        if stamp.tzinfo is None:
            stamp=stamp.replace(tzinfo=CHINA_TIME)
        return stamp.astimezone(CHINA_TIME)

    def _insert_success(self, db, target, digest, task, stamp):
        db.execute('INSERT OR IGNORE INTO published VALUES (?,?,?,?,?)',
                   (target,digest,stamp.date().isoformat(),task,stamp.isoformat()))

    def _import_success_history(self, db, stop_requested):
        marker='confirmed_success_history_v1'
        if db.execute('SELECT 1 FROM metadata WHERE key=?',(marker,)).fetchone():
            return
        with db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM metadata WHERE key=?',(marker,)).fetchone():
                return
            for path in sorted((self.path.parent/'publish_reports').glob('*.json')):
                if stop_requested():
                    raise InterruptedError('计数迁移已取消')
                report=json.loads(path.read_text(encoding='utf-8-sig'))
                task=report.get('task_id',path.name)
                for item in report.get('files',[]):
                    for target,result in item.get('results',{}).items():
                        attempts=result.get('attempts',[])
                        successes=[a for a in attempts if a.get('status')=='SUCCESS']
                        # A persisted acknowledgement can precede attempt finalization.
                        if not successes and result.get('status')=='SUCCESS':
                            started=[a for a in attempts if a.get('upload_started') is True]
                            successes=started[-1:]
                        for attempt in successes:
                            timestamp=(attempt.get('finished_at') or result.get('updated_at')
                                       or attempt.get('upload_started_at') or attempt.get('started_at'))
                            digest=item.get('sha256','')
                            if not timestamp or not digest:
                                continue
                            self._insert_success(db,target,digest,task,self._stamp(timestamp))
            # Recover a success saved just before a crash, before its report was flushed.
            ledger=self.path.parent/'target_ledger.json'
            if ledger.is_file():
                results=json.loads(ledger.read_text(encoding='utf-8-sig')).get('results',{})
                for digest,targets in results.items():
                    if stop_requested():
                        raise InterruptedError('计数迁移已取消')
                    for target,result in targets.items():
                        if (result.get('status')=='SUCCESS' and result.get('task_id')
                                and result.get('attempt_count') and result.get('updated_at')):
                            self._insert_success(db,target,digest,result['task_id'],self._stamp(result['updated_at']))
            db.execute('INSERT INTO metadata VALUES (?,?)',(marker,'1'))

    def snapshot(self, target, default=10):
        day = self.day()
        if not self.path.exists() and not (self.path.parent/'publish_reports').exists() and not (self.path.parent/'target_ledger.json').exists():
            return {'day': day, 'limit': default, 'used': 0, 'remaining': default}
        with self.connect() as db:
            limit = db.execute('SELECT maximum FROM limits WHERE target=?', (target,)).fetchone()
            used = db.execute('SELECT COUNT(*) FROM published WHERE day=? AND target=?', (day, target)).fetchone()[0]
        maximum = limit[0] if limit else default
        return {'day': day, 'limit': maximum, 'used': used, 'remaining': max(0, maximum-used)}

    def reserve(self, target, digest, task, attempt_id, default=10):
        """Check the success allowance before transfer; starting a transfer spends none.

        The publisher serializes browser tasks. Keep uploads as a transfer audit,
        including old versions' rows; only record_success consumes the allowance.
        """
        stamp = self.clock().astimezone(CHINA_TIME)
        day = stamp.date().isoformat()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing=db.execute('SELECT started_at FROM uploads WHERE id=?', (attempt_id,)).fetchone()
            if existing:
                return existing[0]
            limit = db.execute('SELECT maximum FROM limits WHERE target=?', (target,)).fetchone()
            maximum = limit[0] if limit else default
            used = db.execute('SELECT COUNT(*) FROM published WHERE day=? AND target=?', (day, target)).fetchone()[0]
            if used >= maximum:
                raise DailyQuotaReached(f'今日已成功发布{used}个，上限{maximum}个；北京时间次日00:00或提高本账号上限后继续')
            db.execute('INSERT INTO uploads VALUES (?,?,?,?,?,?)',
                       (attempt_id, day, target, digest, task, stamp.isoformat()))
            return stamp.isoformat()

    def record_success(self, target, digest, task='', confirmed_at=None):
        """Count one confirmed content publication once, across retries and restarts."""
        with self.connect() as db:
            self._insert_success(db,target,digest,task,self._stamp(confirmed_at))
