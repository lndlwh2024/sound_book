"""Serial multi-target publishing with persistent per-target results and deferred archive."""
import hashlib
import json
import shutil
import threading
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone, timedelta
from pathlib import Path

from PySide6.QtCore import QThread, Signal
from .platforms import ROOT
from .douyin.daemon import is_file_fully_written
from .media import selected_extensions, media_type
from .douyin.uploader import DouyinUploader, compute_file_md5
from .youtube import YouTubeUploader

CHINA_TIME = timezone(timedelta(hours=8))
SUCCESS = 'SUCCESS'
BLOCKING = {'ACCOUNT_LIMIT', 'AUTH_REQUIRED', 'IDENTITY_MISMATCH', 'CONFIG_REQUIRED'}
RETRYABLE = {'FAILED'}


def now():
    return datetime.now(CHINA_TIME).isoformat(timespec='seconds')


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


@dataclass
class PublishTarget:
    platform: str
    account: object
    profile: str

    @property
    def key(self):
        return self.platform + ':' + self.account.account_id


class MultiTargetLedger:
    def __init__(self, root):
        self.path = Path(root) / 'target_ledger.json'
        self.data = {'schema_version': 1, 'results': {}, 'blocked': {}, 'plans': {}}
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding='utf-8'))  # Fail closed; never discard success records.
            if self.data.get('schema_version') != 1:
                raise ValueError('发布记录版本不支持，保留文件并停止任务')
            self.data.setdefault('plans', {})

    def register_plan(self, digest, targets):
        plan = self.data['plans'].setdefault(digest, {'required': [], 'archive_enabled': {}})
        for target in targets:
            if target.key not in plan['required']:
                plan['required'].append(target.key)
            plan['archive_enabled'][target.key] = getattr(target.account, 'auto_archive', True)
        atomic_json(self.path, self.data)
        return plan

    def result(self, digest, key):
        return self.data['results'].get(digest, {}).get(key, {})

    def set_result(self, digest, key, status, message, info=None):
        item = {'status': status, 'message': message, 'info': info, 'updated_at': now()}
        self.data['results'].setdefault(digest, {})[key] = item
        atomic_json(self.path, self.data)
        return item

    def block(self, key, status, message):
        self.data['blocked'][key] = {'status': status, 'message': message,
                                      'date': datetime.now(CHINA_TIME).date().isoformat()}
        atomic_json(self.path, self.data)

    def quota_block(self, key):
        item = self.data['blocked'].get(key, {})
        return item if item.get('status') == 'ACCOUNT_LIMIT' and item.get('date') == datetime.now(CHINA_TIME).date().isoformat() else None


class DeferredDouyinUploader(DouyinUploader):
    """Add a durable submission boundary only for multi-target tasks, leaving legacy untouched."""
    before_submit = staticmethod(lambda info: None)

    def _get_publish_button(self, page):
        button = super()._get_publish_button(page)
        uploader = self
        class SubmitButton:
            def click(self, *args, **kwargs):
                uploader.before_submit({'platform': 'douyin'})
                uploader._check_stop()
                return button.click(*args, **kwargs)
        return SubmitButton()


class MultiDeployWorker(QThread):
    log = Signal(str)
    queue_changed = Signal()
    status = Signal(str)
    result_changed = Signal(dict)

    def __init__(self, targets, root=None, parent=None, uploader_factory=None, poll_seconds=300, interval_seconds=30, file_paths=None):
        super().__init__(parent)
        self.targets = [PublishTarget(t.platform, replace(t.account), t.profile) for t in targets]
        self.root = Path(root) if root else ROOT / 'data' / 'multi_platform'
        self.factory = uploader_factory or self.make_uploader
        self.poll_seconds, self.interval_seconds = poll_seconds, interval_seconds
        self.stopping = threading.Event()
        self.blocked = {}
        self.processed = set()
        self.file_paths = None if file_paths is None else {str(Path(p).resolve()) for p in file_paths}
        self.report = {'task_id': uuid.uuid4().hex, 'started_at': now(), 'status': 'RUNNING',
                       'files': [], 'targets': [{'key': t.key, 'platform': t.platform,
                        'account_name': t.account.account_name, 'directory': t.account.target_dir} for t in self.targets]}
        self.report_path = self.root / 'publish_reports' / ('publish_task_' + datetime.now(CHINA_TIME).strftime('%Y%m%d_%H%M%S') + '_' + self.report['task_id'][:8] + '.json')

    def stop(self):
        self.stopping.set()
        self.status.emit('正在停止当前任务；已提交但未确认的目标将保留待核对')

    def make_uploader(self, target, log):
        if target.platform == 'douyin':
            # Existing browser operations unchanged; archive is owned by this task.
            return DeferredDouyinUploader(replace(target.account, auto_archive=False), stop_requested=self.stopping.is_set,
                                 progress_callback=log)
        if target.platform == 'youtube':
            return YouTubeUploader(target.account, target.profile, self.root / 'dom_reports',
                                   self.stopping.is_set, log)
        raise ValueError('该平台尚未通过现场DOM验证，不能执行发布')

    def save_report(self):
        entries = [r for f in self.report['files'] for r in f['results'].values()]
        self.report['source_file_count'] = len(self.report['files'])
        self.report['target_count'] = len(entries)
        self.report['success_count'] = sum(r['status'] == SUCCESS for r in entries)
        self.report['failure_count'] = sum(r['status'] not in {SUCCESS, 'PENDING', 'IN_PROGRESS', 'UNCONFIRMED', 'CANCELLED'} for r in entries)
        self.report['unconfirmed_count'] = sum(r['status'] == 'UNCONFIRMED' for r in entries)
        self.report['cancelled_count'] = sum(r['status'] == 'CANCELLED' for r in entries)
        failed = lambda item:any(r['status'] not in {SUCCESS,'PENDING','IN_PROGRESS','UNCONFIRMED','CANCELLED'} for r in item['results'].values())
        summary = lambda item:{key:item[key] for key in ('file_name','file_path','size_bytes','modified_at','results','archived_path')}
        self.report['success_files'] = [summary(f) for f in self.report['files'] if all(r['status']==SUCCESS for r in f['results'].values())]
        self.report['failure_files'] = [summary(f) for f in self.report['files'] if failed(f)]
        self.report['source_success_count'] = len(self.report['success_files'])
        self.report['source_failure_count'] = len(self.report['failure_files'])
        self.report['platform_counts'] = {}
        for file in self.report['files']:
            for key, result in file['results'].items():
                counts = self.report['platform_counts'].setdefault(key.split(':')[0], {'success': 0, 'failure': 0, 'unconfirmed': 0, 'cancelled': 0})
                state = result['status']
                counts['success' if state == SUCCESS else 'unconfirmed' if state == 'UNCONFIRMED' else 'cancelled' if state == 'CANCELLED' else 'failure'] += state not in {'PENDING','IN_PROGRESS'}
        atomic_json(self.report_path, self.report)
        for item in self.report['files']:
            self.result_changed.emit(item)

    def fingerprint(self, path):
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                if self.stopping.is_set():
                    return None
                digest.update(chunk)
        return digest.hexdigest()

    def plan_batch(self):
        files = {}
        for target in self.targets:
            directory = Path(target.account.target_dir)
            if not target.account.target_dir or not directory.is_dir():
                self.log.emit(f'[目录无效] {target.key}：{directory}')
                continue
            allowed = selected_extensions(target.account)
            for path in sorted(directory.iterdir(), key=lambda p: p.stat().st_mtime if p.exists() else 0):
                if path.suffix.lower() not in allowed or not path.is_file() or not is_file_fully_written(path):
                    continue
                resolved = str(path.resolve())
                if self.file_paths is not None and resolved not in self.file_paths:
                    continue
                files.setdefault(resolved, {'path': path, 'targets': []})['targets'].append(target)
        jobs = []
        for resolved, job in files.items():
            stat = job['path'].stat()
            signature = (stat.st_size, stat.st_mtime_ns)
            if (resolved, signature) in self.processed:
                continue
            digest = self.fingerprint(job['path'])
            if digest is None:
                break
            # Producer may have changed the file while hashing.
            after = job['path'].stat()
            if signature != (after.st_size, after.st_mtime_ns):
                continue
            self.processed.add((resolved, signature))
            job.update(digest=digest, signature=signature)
            plan = self.ledger.register_plan(digest, job['targets'])
            job['plan'] = plan
            item = {'file_name': job['path'].name, 'file_path': resolved, 'size_bytes': stat.st_size,
                    'sha256': digest, 'mtime_ns': stat.st_mtime_ns,
                    'modified_at': datetime.fromtimestamp(stat.st_mtime, CHINA_TIME).isoformat(),
                    'results': {key: {**self.ledger.result(digest, key), 'status':self.ledger.result(digest,key).get('status','PENDING'),
                        'attempts': []} for key in plan['required']}, 'archived_path': None}
            job['item'] = item
            self.report['files'].append(item)
            jobs.append(job)
        self.save_report()
        return jobs

    def seed_douyin_success(self, job, target):
        if target.platform != 'douyin':
            return None
        records = Path(target.account.target_dir) / 'reports'
        digest = None
        for path in records.glob('publish_ledger_*.json'):
            try:
                records_data = json.loads(path.read_text(encoding='utf-8-sig'))
                if not isinstance(records_data, list) or any(not isinstance(r,dict) for r in records_data):
                    raise ValueError('账本结构应为记录列表')
            except (ValueError, OSError) as error:
                raise RuntimeError(f'原抖音账本读取失败：{path}；{type(error).__name__}: {error}；停止该账号以避免重复投稿') from error
            for record in records_data:
                if record.get('account_id') == target.account.account_id and record.get('status') == SUCCESS:
                    digest = digest or compute_file_md5(job['path'])
                    if record.get('file_hash') == digest:
                        return self.ledger.set_result(job['digest'], target.key, 'UNCONFIRMED',
                            '发现原抖音成功账本；旧记录缺少本次确认依据，请核对后标记成功或允许重试，避免重复发布', record)
        return None

    def attempt(self, job, target):
        item = job['item']['results'][target.key]
        previous = self.ledger.result(job['digest'], target.key)
        if previous and previous.get('status') in (SUCCESS, 'UNCONFIRMED', 'PERMANENT_REJECT', 'DURATION_LIMIT'):
            if previous.get('status') == 'UNCONFIRMED' and target.platform == 'youtube':
                try:
                    checker = self.factory(target, self.log.emit)
                    if checker.reconcile(previous.get('info')):
                        previous = self.ledger.set_result(job['digest'], target.key, SUCCESS,
                            '只读核对确认之前提交已生效，未重复上传', previous['info'])
                except Exception as error:
                    self.log.emit(f'[待核对] {target.key}：{error}；不重复提交')
            item.update(previous)
            self.log.emit(f'[哈希去重] {job["path"].name} → {target.key}；SHA-256={job["digest"]}；已有状态{previous["status"]}，不重复上传')
            self.save_report()
            return item['status']
        blocked = self.blocked.get(target.key) or self.ledger.quota_block(target.key)
        if blocked:
            item.update(status=blocked['status'], message=blocked['message'])
            self.save_report()
            return item['status']
        if self.stopping.is_set():
            item.update(status='CANCELLED', message='用户停止，未执行目标')
            self.save_report()
            return 'CANCELLED'
        path = job['path']
        if not is_file_fully_written(path) or (path.stat().st_size, path.stat().st_mtime_ns) != job['signature']:
            item.update(status='FILE_CHANGED', message='文件变化或仍被占用，未上传')
            self.save_report()
            return 'FILE_CHANGED'
        if target.platform == 'youtube' and media_type(path) != 'video':
            item.update(self.ledger.set_result(job['digest'],target.key,'PERMANENT_REJECT',
                'YouTube 当前发布接口仅支持视频，独立音频/图片不上传、不重试，保留源文件'))
            self.save_report()
            return item['status']
        if not previous:
            try:
                historical = self.seed_douyin_success(job, target)
            except RuntimeError as error:
                message = str(error)
                self.blocked[target.key] = {'status':'CONFIG_REQUIRED','message':message}
                item.update(status='CONFIG_REQUIRED',message=message)
                self.log.emit(f'[停止该账号] {target.key}：{message}；其他目标继续')
                self.save_report()
                return 'CONFIG_REQUIRED'
            except OSError as error:
                item.update(status='FILE_CHANGED', message=f'读取媒体失败：{path}；{error}；未上传')
                self.save_report()
                return 'FILE_CHANGED'
            if historical:
                item.update(historical)
                self.log.emit(f'[历史去重待核对] {path.name} → {target.key}；未重复上传')
                self.save_report()
                return historical['status']
        attempt = {'number': len(item['attempts']) + 1, 'started_at': now(), 'logs': []}
        item['attempts'].append(attempt)
        item['status'] = 'IN_PROGRESS'
        self.save_report()
        def log(message):
            attempt['logs'].append({'time': now(), 'message': message})
            del attempt['logs'][:-100]
            self.log.emit(f'[{target.platform}/{target.account.account_name}] {message}')
            self.save_report()
        self.status.emit(f'{target.platform}/{target.account.account_name}：{path.name}')
        try:
            uploader = self.factory(target, log)
            def before_submit(info):
                self.ledger.set_result(job['digest'], target.key, 'UNCONFIRMED', '已进入提交边界；中断后必须核对平台结果', info)
                item.update(status='UNCONFIRMED', message='已进入提交边界，等待平台确认', info=info)
                self.save_report()
            uploader.before_submit = before_submit
            success, message, info = uploader.upload_single_video(path)
            state = SUCCESS if success else uploader.last_outcome
            if hasattr(info, 'to_dict'):
                info = info.to_dict()
        except Exception as error:
            boundary = self.ledger.result(job['digest'], target.key)
            state = 'UNCONFIRMED' if boundary.get('status') == 'UNCONFIRMED' else 'FAILED'
            message = str(error)
            info = boundary.get('info') if state == 'UNCONFIRMED' else None
        recorded = self.ledger.set_result(job['digest'], target.key, state, message, info)
        item.update(recorded)
        attempt.update(status=state, finished_at=now(), message=message, info=info)
        if state in BLOCKING:
            self.blocked[target.key] = {'status': state, 'message': message}
            if state == 'ACCOUNT_LIMIT':
                self.ledger.block(target.key, state, message)
            self.log.emit(f'[停止该账号] {target.key}：{message}；其他目标继续')
        self.save_report()
        return state

    def archive(self, job):
        item = job['item']
        if (self.stopping.is_set() or item['archived_path'] or
            not all(r['status'] == SUCCESS for r in item['results'].values()) or
            not all(job['plan']['archive_enabled'].values())):
            return
        source = job['path']
        if not is_file_fully_written(source) or (source.stat().st_size, source.stat().st_mtime_ns) != job['signature']:
            item['archive_error'] = '全部目标成功，但原文件已变化或被占用；保留文件'
            self.save_report()
            return
        destination = source.parent / 'published' / source.name
        if destination.exists():
            item['archive_error'] = '归档存在同名文件，未覆盖'
            self.save_report()
            return
        destination.parent.mkdir(exist_ok=True)
        try:
            shutil.move(str(source), str(destination))
            item['archived_path'] = str(destination)
            subtitle = source.with_suffix('.srt')
            if subtitle.exists() and not destination.with_suffix('.srt').exists():
                shutil.move(str(subtitle), str(destination.with_suffix('.srt')))
            self.log.emit(f'[统一归档] {source.name}：全部{len(item["results"])}个目标已确认成功')
        except OSError as error:
            item['archive_error'] = str(error)
        self.save_report()
        self.queue_changed.emit()

    def run_batch(self, jobs):
        retries = []
        for job in jobs:
            for target in job['targets']:
                before = len(job['item']['results'][target.key]['attempts'])
                state = self.attempt(job, target)
                if state in RETRYABLE:
                    retries.append((job, target))
                if self.stopping.is_set():
                    break
                if len(job['item']['results'][target.key]['attempts']) > before and state not in BLOCKING | {'DURATION_LIMIT','PERMANENT_REJECT','FILE_CHANGED'}:
                    self.stopping.wait(self.interval_seconds)
            self.archive(job)
        for job, target in retries:
            if self.stopping.is_set():
                break
            self.log.emit(f'[集中重试一次] {job["path"].name} → {target.key}')
            self.attempt(job, target)
            self.archive(job)
            self.stopping.wait(self.interval_seconds)

    def run(self):
        try:
            self.ledger = MultiTargetLedger(self.root)
            self.save_report()
            self.log.emit(f'[任务报告] {self.report_path}')
            while not self.stopping.is_set():
                self.run_batch(self.plan_batch())
                if self.file_paths is not None:
                    self.report['status'] = 'STOPPED' if self.stopping.is_set() else 'COMPLETED'
                    break
                if all(t.key in self.blocked or self.ledger.quota_block(t.key) for t in self.targets):
                    self.report['status'] = 'BLOCKED'
                    break
                self.status.emit('本轮结束，等待新文件；失败目标仅尝试两轮，未确认目标保留待核对')
                self.stopping.wait(self.poll_seconds)
        except Exception as error:
            self.report.update(status='FAILED', error=str(error))
            self.log.emit(f'[任务异常] {error}')
        finally:
            if self.report['status'] == 'RUNNING':
                self.report['status'] = 'STOPPED'
            self.report['finished_at'] = now()
            for file in self.report['files']:
                for result in file['results'].values():
                    if result['status'] == 'PENDING':
                        result.update(status='CANCELLED', message='本次任务结束前尚未执行')
                    elif result['status'] == 'IN_PROGRESS':
                        result.update(status='UNCONFIRMED', message='任务异常，可能已提交，需核对')
            try:
                self.save_report()
                self.log.emit(f'[任务结束] 成功{self.report["success_count"]}，失败{self.report["failure_count"]}，未确认{self.report["unconfirmed_count"]}；{self.report_path}')
            except Exception as error:
                self.log.emit(f'[报告保存失败] {error}')
