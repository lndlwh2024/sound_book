"""Serial multi-target publishing with persistent per-target results and deferred archive."""
import hashlib
import json
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone, timedelta
from pathlib import Path

from PySide6.QtCore import QThread, Signal
from .platforms import ROOT, CHANNELS
from .douyin.daemon import is_file_fully_written
from .media import selected_extensions, media_type
from .douyin.uploader import DouyinUploader, compute_file_md5
from .youtube import YouTubeUploader
from .creator_studio import XiaohongshuUploader, KuaishouUploader
from .status import BLOCKING, RETRYABLE, result_status_text, fresh_result
from .upload_quota import DailyUploadQuota, DailyQuotaReached

CHINA_TIME = timezone(timedelta(hours=8))
SUCCESS = 'SUCCESS'
LEGACY_REVIEW_MESSAGE = '发现原抖音成功账本；旧记录缺少本次确认依据，请核对后标记成功或允许重试，避免重复发布'
LEGACY_FAILED_MESSAGE = '旧未确认投稿按失败处理；新任务允许重试'


def legacy_import_needed(previous):
    return not previous or (previous.get('status')=='FAILED' and previous.get('message')==LEGACY_FAILED_MESSAGE)


def now():
    return datetime.now(CHINA_TIME).isoformat(timespec='seconds')


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    for attempt in range(6):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt==5:
                raise
            # Windows readers/security scanners can hold the destination briefly.
            time.sleep(.05*(attempt+1))


def archive_completed_file(source,signature,log=lambda message:None,cloud_root=None):
    """Archive without overwriting previous media or subtitles."""
    source=Path(source)
    if not is_file_fully_written(source) or (source.stat().st_size,source.stat().st_mtime_ns)!=signature:
        raise ValueError('目标已完成，但原文件已变化或被占用；保留文件')
    folder, name = 'published', source.name
    if cloud_root and (Path(cloud_root)/'drive_ingest.sqlite3').exists():
        from .drive_ingest import DriveJournal, file_hashes
        digest, _ = file_hashes(source)
        origins = DriveJournal(cloud_root).origins(source, digest)
        if origins:
            folder = 'publiced'
            if any(entry['duplicate'] for entry in origins):
                name = source.stem + '_重复' + source.suffix
    destination=source.parent/folder/name
    if destination.exists() or destination.with_suffix('.srt').exists():
        destination=destination.with_name(destination.stem+'_'+uuid.uuid4().hex[:12]+source.suffix)
    destination.parent.mkdir(exist_ok=True)
    shutil.move(str(source),str(destination))
    subtitle=source.with_suffix('.srt')
    if subtitle.exists() and not destination.with_suffix('.srt').exists():
        try:
            shutil.move(str(subtitle),str(destination.with_suffix('.srt')))
        except OSError as error:
            log(f'[字幕归档未完成] 媒体已归档，字幕仍保留：{error}')
    return destination


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

    def register_plan(self, digest, targets, fixed=False):
        plan = self.data['plans'].setdefault(digest, {'required': [], 'archive_enabled': {}})
        if fixed:
            plan['required'] = [t.key for t in targets]
            plan['archive_enabled'] = {}
        for target in targets:
            if target.key not in plan['required']:
                plan['required'].append(target.key)
            plan['archive_enabled'][target.key] = getattr(target.account, 'auto_archive', True)
        atomic_json(self.path, self.data)
        return plan

    def result(self, digest, key):
        return self.data['results'].get(digest, {}).get(key, {})

    def set_result(self, digest, key, status, message, info=None, **metadata):
        item = {'status': status, 'message': message, 'info': info, 'updated_at': now(), **metadata}
        self.data['results'].setdefault(digest, {})[key] = item
        atomic_json(self.path, self.data)
        return item

    def restore_legacy_success(self,digest,key,path,signature=None):
        """Locally verify a migration downgrade, usable before any deployment."""
        previous=self.result(digest,key)
        if (not key.startswith('douyin:') or previous.get('status')!='UNCONFIRMED'
                or previous.get('message')!=LEGACY_REVIEW_MESSAGE):
            return previous
        record=previous.get('info')
        if (not isinstance(record,dict) or record.get('status')!=SUCCESS
                or record.get('account_id')!=key.split(':',1)[1]):
            return previous
        path=Path(path)
        try:
            if not is_file_fully_written(path):
                return previous
            before=path.stat()
            observed=(before.st_size,before.st_mtime_ns)
            if signature is not None and observed!=signature:
                return previous
            sha,md5=hashlib.sha256(),hashlib.md5()
            with path.open('rb') as stream:
                for chunk in iter(lambda:stream.read(1024*1024),b''):
                    sha.update(chunk)
                    md5.update(chunk)
            after=path.stat()
            if ((after.st_size,after.st_mtime_ns)!=observed or sha.hexdigest()!=digest
                    or md5.hexdigest()!=record.get('file_hash')):
                return previous
        except OSError:
            return previous
        return self.set_result(digest,key,SUCCESS,
            '修正旧成功账本迁移状态；同账号、SHA-256及原MD5匹配，沿用历史成功，不重复上传',record)

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

    def upload_single_video(self,*args,**kwargs):
        success,message,info=super().upload_single_video(*args,**kwargs)
        if not success and info is None:
            info={'platform':'douyin','account_id':self.account.account_id,
                  'confirmation':getattr(self,'_publish_result',None)}
        return success,message,info

    def reconcile(self,info):
        return bool(isinstance(info,dict) and info.get('platform')=='douyin'
            and info.get('account_id')==self.account.account_id
            and isinstance(info.get('confirmation'),dict)
            and str(info['confirmation'].get('code'))=='0')

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

    def __init__(self, targets, root=None, parent=None, uploader_factory=None, poll_seconds=300, interval_seconds=30, file_paths=None, archive_targets=None, deferred_preflight=None, history_targets=None):
        super().__init__(parent)
        self.targets = [PublishTarget(t.platform, replace(t.account), t.profile)
            for t in sorted(targets,key=lambda t:(list(CHANNELS).index(t.platform),t.account.account_id))]
        self.root = Path(root) if root else ROOT / 'data' / 'multi_platform'
        # Completion belongs to this task. Older saved archive groups only supply
        # history for the other platform columns; they cannot block this task.
        self.archive_targets = self.targets
        self.history_targets = [PublishTarget(t.platform,replace(t.account),t.profile)
            for t in (history_targets if history_targets is not None else archive_targets or targets)]
        self.quota = DailyUploadQuota(self.root)
        self.attempts_by_target = {}
        self.jobs_cache = {}
        self.quota_notices = {}
        self.deferred_preflight = set(deferred_preflight or ())
        self.factory = uploader_factory or self.make_uploader
        self.poll_seconds, self.interval_seconds = poll_seconds, interval_seconds
        self.stopping = threading.Event()
        self.pause_requested = threading.Event()
        self.wake_requested = threading.Event()
        self.blocked = {}
        self.target_enabled = {t.key: t.account.enabled for t in self.targets}
        self.processed = set()
        self.file_paths = None if file_paths is None else {str(Path(p).resolve()) for p in file_paths}
        self.report = {'task_id': uuid.uuid4().hex, 'started_at': now(), 'status': 'RUNNING',
                       'files': [], 'targets': [{'key': t.key, 'platform': t.platform,
                        'account_name': t.account.account_name, 'directory': t.account.target_dir} for t in self.targets]}
        self.report_path = self.root / 'publish_reports' / ('publish_task_' + datetime.now(CHINA_TIME).strftime('%Y%m%d_%H%M%S') + '_' + self.report['task_id'][:8] + '.json')

    def stop(self):
        self.stopping.set()
        self.wake_requested.set()
        self.pause_requested.clear()
        self.status.emit('正在停止当前任务；未确认成功的上传记录为失败，保留原文件')

    def pause_all(self):
        if self.stopping.is_set():
            return
        self.pause_requested.set()
        self.log.emit('[全平台暂停] 已请求暂停；当前文件完成后暂停，不启动后续文件和重试；浏览器上传与发布确认继续')
        self.status.emit('已请求暂停全部平台；等待当前文件结束后进入暂停，可点击继续或停止')

    def resume(self):
        self.pause_requested.clear()
        self.log.emit('[全平台继续] 继续同一任务及报告，沿用已有发布结果')
        self.status.emit('全部平台已继续部署')

    def checkpoint(self):
        """Pause at file boundaries, leaving active browser transfers / confirmation intact."""
        announced = False
        while self.pause_requested.is_set() and not self.stopping.is_set():
            if not announced:
                self.report['status'] = 'PAUSED'
                self.save_report()
                self.status.emit('全部平台已暂停；不会开始后续文件或重试，可继续当前任务')
                self.log.emit('[全平台暂停] 已进入暂停；保留任务与已有结果')
                announced = True
            self.stopping.wait(.1)
        if announced and not self.stopping.is_set():
            self.report['status'] = 'RUNNING'
            self.save_report()
        return not self.stopping.is_set()

    def controlled_wait(self, seconds):
        remaining = seconds
        while remaining > 0 and self.checkpoint():
            started = time.monotonic()
            if self.wake_requested.wait(min(.1, remaining)):
                self.wake_requested.clear()
                break
            remaining -= time.monotonic() - started

    def make_uploader(self, target, log):
        if target.platform == 'douyin':
            # Existing browser operations unchanged; archive is owned by this task.
            return DeferredDouyinUploader(replace(target.account, auto_archive=False), stop_requested=self.stopping.is_set,
                                 progress_callback=log)
        if target.platform == 'youtube':
            return YouTubeUploader(target.account, target.profile, self.root / 'dom_reports',
                                   self.stopping.is_set, log)
        if target.platform in ('xiaohongshu','kuaishou'):
            adapter=XiaohongshuUploader if target.platform=='xiaohongshu' else KuaishouUploader
            return adapter(target.account,target.profile,self.root/'dom_reports',self.stopping.is_set,log)
        raise ValueError('该平台发布执行器尚未实现；DOM 采集状态与发布支持是独立检查项')

    def save_report(self):
        required={t.key for t in self.targets}
        keys_for = lambda item:set(item.get('task_targets',required))
        entries = [r for f in self.report['files'] for key,r in f['results'].items() if key in keys_for(f)]
        self.report['source_file_count'] = len(self.report['files'])
        self.report['target_count'] = len(entries)
        self.report['success_count'] = sum(r['status'] == SUCCESS for r in entries)
        self.report['failure_count'] = sum(r['status'] in {'FAILED','FAILED1','FAILED2'} for r in entries)
        self.report['unconfirmed_count'] = 0  # Kept for old report readers.
        self.report['ignored_count'] = sum(r['status']=='IGNORED' for r in entries)
        self.report['cancelled_count'] = sum(r['status'] == 'CANCELLED' for r in entries)
        failed = lambda item:any(r['status'] in {'FAILED','FAILED1','FAILED2'} for key,r in item['results'].items() if key in keys_for(item))
        summary = lambda item:{key:item[key] for key in ('file_name','file_path','size_bytes','modified_at','results','archived_path')}
        self.report['success_files'] = [summary(f) for f in self.report['files']
            if all(f['results'].get(key,{}).get('status') in (SUCCESS,'IGNORED') for key in keys_for(f))]
        self.report['failure_files'] = [summary(f) for f in self.report['files'] if failed(f)]
        self.report['source_success_count'] = len(self.report['success_files'])
        self.report['source_failure_count'] = len(self.report['failure_files'])
        self.report['platform_counts'] = {}
        for file in self.report['files']:
            for key, result in file['results'].items():
                if key not in keys_for(file):
                    continue
                counts = self.report['platform_counts'].setdefault(key.split(':')[0], {'success': 0, 'failure': 0, 'unconfirmed': 0, 'cancelled': 0, 'ignored': 0, 'blocked':0, 'waiting':0})
                state = result['status']
                category=('success' if state==SUCCESS else 'failure' if state in ('FAILED','FAILED1','FAILED2')
                    else 'cancelled' if state=='CANCELLED' else 'ignored' if state=='IGNORED'
                    else 'waiting' if state in ('PENDING','IN_PROGRESS','RETRYING') else 'blocked')
                counts[category]+=1
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
            for path in directory.iterdir():
                if path.suffix.lower() not in allowed or not path.is_file() or not is_file_fully_written(path):
                    continue
                resolved = str(path.resolve())
                if self.file_paths is not None and resolved not in self.file_paths:
                    continue
                try:
                    modified=path.stat().st_mtime_ns
                except OSError:
                    continue
                files.setdefault(resolved, {'path': path, 'targets': [],'modified':modified})['targets'].append(target)
        jobs = []
        for resolved, job in sorted(files.items(),key=lambda entry:(entry[1]['modified'],
                entry[1]['path'].name.casefold(),entry[0].casefold())):
            try:
                stat = job['path'].stat()
            except OSError:
                continue
            signature = (stat.st_size, stat.st_mtime_ns)
            if (resolved, signature) in self.jobs_cache:
                existing = self.jobs_cache[(resolved,signature)]
                existing['targets'] = job['targets']
                existing['item']['results'].update(self.reconcile_archive_history(existing))
                jobs.append(existing)
                continue
            try:
                digest = self.fingerprint(job['path'])
                after = job['path'].stat()
            except OSError as error:
                self.log.emit(f'[文件暂不能读取] {job["path"].name}：{error}；下次扫描再处理')
                continue
            if digest is None:
                break
            # Producer may have changed the file while hashing.
            if signature != (after.st_size, after.st_mtime_ns):
                continue
            self.processed.add((resolved, signature))
            job.update(digest=digest, signature=signature)
            archive_history=self.reconcile_archive_history(job)
            plan = self.ledger.register_plan(digest, self.archive_targets, fixed=True)
            job['plan'] = plan
            keys = list(dict.fromkeys(plan['required']+[t.key for t in self.history_targets]))
            item = {'file_name': job['path'].name, 'file_path': resolved, 'size_bytes': stat.st_size,
                    'sha256': digest, 'mtime_ns': stat.st_mtime_ns,
                    'modified_at': datetime.fromtimestamp(stat.st_mtime, CHINA_TIME).isoformat(),
                    'results': {key: fresh_result(self.ledger.result(digest,key)) for key in keys}, 'archived_path': None}
            item['results'].update(archive_history)
            for key,result in item['results'].items():
                result['attempts'] = self.attempts_by_target.setdefault((digest,key),[])
            job['item'] = item
            self.jobs_cache[(resolved,signature)] = job
            self.report['files'].append(item)
            jobs.append(job)
        self.save_report()
        return jobs

    def reconcile_archive_history(self,job):
        """Restore old successes even when this task only uploads other platforms."""
        active={t.key for t in job['targets']}
        restored={}
        for target in self.history_targets:
            if target.platform!='douyin' or target.key in active:
                continue
            if not self.checkpoint():
                break
            try:
                previous=self.restore_legacy_success(job,target,self.ledger.result(job['digest'],target.key))
                if legacy_import_needed(previous) and target.key not in self.blocked:
                    historical=self.seed_douyin_success(job,target)
                    if historical:
                        previous=historical
                        self.log.emit(f'[历史成功迁移] {job["path"].name} → {target.key}：归档目标的旧成功记录已恢复，未重新上传')
                if previous:
                    restored[target.key]=fresh_result(previous)
            except RuntimeError as error:
                self.blocked[target.key]={'status':'CONFIG_REQUIRED','message':str(error)}
                self.log.emit(f'[历史核对失败] {target.key}：{error}；其他上传账号继续，保留文件')
            except OSError as error:
                restored[target.key]={'status':'FILE_CHANGED','message':str(error),'attempts':[]}
            if target.key in self.blocked:
                restored[target.key]={**self.blocked[target.key],'attempts':[]}
        return restored

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
                        after=job['path'].stat()
                        if (after.st_size,after.st_mtime_ns)!=job['signature']:
                            raise OSError('匹配旧账本时文件发生变化，未迁移成功记录')
                        return self.ledger.set_result(job['digest'], target.key, SUCCESS,
                            '沿用原抖音成功记录；同账号及文件MD5匹配，已迁移到SHA-256去重，不重复上传', record)
        return None

    def restore_legacy_success(self,job,target,previous):
        """Repair only the prior migration downgrade, never a real uncertain post."""
        result=self.ledger.restore_legacy_success(job['digest'],target.key,job['path'],job['signature'])
        if result.get('status')==SUCCESS and previous.get('status')!='SUCCESS':
            self.log.emit(f'[历史成功恢复] {job["path"].name} → {target.key}；旧成功记录匹配，未重新投稿')
        return result

    def attempt(self, job, target):
        item = job['item']['results'][target.key]
        if not self.checkpoint():
            if item['status'] not in (SUCCESS, 'IGNORED', 'FAILED1', 'FAILED2'):
                item.update(status='CANCELLED', message='用户停止，未执行目标')
                self.save_report()
            return item['status']
        if not self.target_enabled.get(target.key,True):
            item.update(status='PENDING',message='账号已关闭；保留文件等待重新启用')
            self.save_report()
            return 'PENDING'
        previous = self.restore_legacy_success(job, target, self.ledger.result(job['digest'],target.key))
        if previous.get('status') in (SUCCESS,'IGNORED','PERMANENT_REJECT','DURATION_LIMIT'):
            if (previous.get('status') == SUCCESS and not self.attempts_by_target.get((job['digest'],target.key))
                    and (self.root/'drive_ingest.sqlite3').exists()):
                from .drive_ingest import DriveJournal
                DriveJournal(self.root).mark_duplicate(target.key,job['path'],job['digest'])
            item.update(previous)
            item.pop('quota_message',None)
            item.pop('blocking_message',None)
            self.save_report()
            return previous['status']
        attempts = self.attempts_by_target.setdefault((job['digest'],target.key),[])
        item['attempts'] = attempts
        if previous.get('info') and previous.get('status') in ('UNCONFIRMED','IN_PROGRESS','FAILED','FAILED1','FAILED2'):
            try:
                checker=self.factory(target,self.log.emit)
                reconcile=getattr(checker,'reconcile',None)
                if callable(reconcile) and reconcile(previous['info']) is True:
                    item.update(self.ledger.set_result(job['digest'],target.key,SUCCESS,'自动检查确认先前投稿成功，未重新上传',previous['info']))
                    item.pop('quota_message',None)
                    item.pop('blocking_message',None)
                    self.save_report()
                    return SUCCESS
            except Exception as error:
                self.log.emit(f'[自动检查] {target.key}：{error}；未确认成功，按失败重试规则处理')
        if len(attempts)>=2:
            item.update(status='FAILED2',attempt_count=2,message=previous.get('message','本任务已尝试两次，不再重试'))
            self.save_report()
            return 'FAILED2'
        block=self.blocked.get(target.key) or self.ledger.quota_block(target.key)
        if block:
            if attempts and item['status'] in ('FAILED1','FAILED2'):
                item['blocking_message']=block['message']
            else:
                item.update(status=block['status'],message=block['message'])
            self.save_report()
            return block['status']
        path=job['path']
        try:
            stable=is_file_fully_written(path) and (path.stat().st_size,path.stat().st_mtime_ns)==job['signature']
        except OSError:
            stable=False
        if not stable:
            item.update(status='FILE_CHANGED',message='文件变化、消失或仍被占用，未上传')
            self.save_report()
            return 'FILE_CHANGED'
        if target.platform in ('youtube','xiaohongshu','kuaishou') and media_type(path)!='video':
            item.update(self.ledger.set_result(job['digest'],target.key,'PERMANENT_REJECT','当前自动发布器仅支持视频，保留文件'))
            self.save_report()
            return 'PERMANENT_REJECT'
        if legacy_import_needed(previous):
            try:
                historical=self.seed_douyin_success(job,target)
            except RuntimeError as error:
                self.blocked[target.key]={'status':'CONFIG_REQUIRED','message':str(error)}
                item.update(status='CONFIG_REQUIRED',message=str(error))
                self.save_report()
                return 'CONFIG_REQUIRED'
            except OSError as error:
                item.update(status='FILE_CHANGED',message=str(error))
                self.save_report()
                return 'FILE_CHANGED'
            if historical:
                item.update(historical)
                self.save_report()
                return SUCCESS
        quota=self.quota.snapshot(target.key,getattr(target.account,'daily_upload_limit',10))
        if not quota['remaining']:
            item['quota_message']=f'今日已用{quota["used"]}次，上限{quota["limit"]}次；北京时间次日00:00或提高本账号上限后继续'
            marker=(quota['day'],quota['limit'],quota['used'])
            if self.quota_notices.get(target.key)!=marker:
                self.log.emit(f'[每日限额等待] {target.key}：'+item['quota_message'])
                self.quota_notices[target.key]=marker
            self.save_report()
            return 'DAILY_LIMIT'
        item.pop('quota_message',None)
        if target.key in self.deferred_preflight:
            from .preflight import AccountPreflight, prerequisite_results
            from playwright.sync_api import sync_playwright
            failures=prerequisite_results([target],self.root/'dom_reports')
            if not failures:
                with sync_playwright() as playwright:
                    result=AccountPreflight(target,self.stopping.is_set,self.log.emit).check(playwright)
                failures=[] if result.passed else [result]
            if failures:
                failure=failures[0]
                self.blocked[target.key]={'status':failure.status,'message':failure.message}
                item.update(status=failure.status,message=failure.message)
                self.save_report()
                return failure.status
            self.deferred_preflight.remove(target.key)
        attempt={'number':len(attempts)+1,'id':uuid.uuid4().hex,'started_at':now(),'logs':[],'upload_started':False}
        attempts.append(attempt)
        item['status']='RETRYING' if attempt['number']==2 else 'IN_PROGRESS'
        self.save_report()
        def log(message):
            attempt['logs'].append({'time':now(),'message':message})
            del attempt['logs'][:-100]
            self.log.emit(f'[{target.platform}/{target.account.account_name}] {message}')
            self.save_report()
        quota_error=None
        boundary_info=previous.get('info')
        def before_upload():
            nonlocal quota_error
            if self.stopping.is_set():
                raise RuntimeError('用户停止，尚未开始上传')
            try:
                attempt['upload_started_at']=self.quota.reserve(target.key,job['digest'],self.report['task_id'],attempt['id'],getattr(target.account,'daily_upload_limit',10))
            except DailyQuotaReached as error:
                quota_error=error
                raise
            attempt['upload_started']=True
            usage=self.quota.snapshot(target.key,getattr(target.account,'daily_upload_limit',10))
            self.log.emit(f'[上传计数] {target.platform}/{target.account.account_name}：今日已用{usage["used"]}次，上限{usage["limit"]}次，剩余{usage["remaining"]}次')
            self.save_report()
        def before_submit(info):
            nonlocal boundary_info
            boundary_info=info
            # These adapters validate saved evidence locally. Do not invoke the
            # YouTube browser reconciliation inside its active upload context.
            confirmed=(target.platform in ('xiaohongshu','kuaishou')
                and callable(getattr(uploader,'reconcile',None)) and uploader.reconcile(info) is True)
            state=SUCCESS if confirmed else 'IN_PROGRESS'
            message='平台已确认本次发布成功，回执已保存' if confirmed else '已提交，等待成功证据；中断后按失败处理'
            saved=self.ledger.set_result(job['digest'],target.key,state,message,info,
                task_id=self.report['task_id'],attempt_count=attempt['number'])
            if confirmed:
                item.update(saved)
            else:
                item.update(message='已提交，等待平台确认',info=info)
            self.save_report()
        self.status.emit(f'{target.platform}/{target.account.account_name}：{path.name}')
        try:
            uploader=self.factory(target,log)
            uploader.before_upload=before_upload
            uploader.before_submit=before_submit
            # Injected adapters without a file-input boundary count their invocation.
            if getattr(uploader,'supports_upload_boundary',False) is not True:
                before_upload()
            success,message,info=uploader.upload_single_video(path)
            state=SUCCESS if success else getattr(uploader,'last_outcome','FAILED')
            if not isinstance(state,str):
                state='FAILED'
            if hasattr(info,'to_dict'):
                info=info.to_dict()
        except Exception as error:
            state,message,info='FAILED',str(error),boundary_info
        if quota_error:
            attempts.pop()
            item.update(status='FAILED1' if attempts else 'PENDING',quota_message=str(quota_error))
            self.save_report()
            return 'DAILY_LIMIT'
        saved=self.ledger.result(job['digest'],target.key)
        if (state!=SUCCESS and saved.get('status')==SUCCESS and saved.get('task_id')==self.report['task_id']
                and saved.get('attempt_count')==attempt['number']):
            state,message,info=SUCCESS,'已保存本次平台成功回执，页面中断不改变结果',saved.get('info')
        if self.stopping.is_set() and not attempt['upload_started']:
            state='CANCELLED'
        if state in ('UNCONFIRMED','FAILED') or state=='CANCELLED' and attempt['upload_started']:
            state='FAILED'+str(attempt['number'])
        blocking_state=state if state in BLOCKING else None
        if blocking_state and attempt['upload_started']:
            info=dict(info) if isinstance(info,dict) else {}
            info['account_block']={'status':blocking_state,'message':message}
            state='FAILED'+str(attempt['number'])
        if not attempt['upload_started'] and state in BLOCKING | {'CANCELLED','DURATION_LIMIT','PERMANENT_REJECT'}:
            attempts.pop()
        item.update(self.ledger.set_result(job['digest'],target.key,state,message,info or boundary_info,
            task_id=self.report['task_id'],attempt_count=len(attempts)))
        attempt.update(status=state,finished_at=now(),message=message,info=info)
        account_block=(info or {}).get('account_block') if isinstance(info,dict) else None
        if blocking_state:
            self.blocked[target.key]={'status':blocking_state,'message':message}
            if blocking_state=='ACCOUNT_LIMIT':
                self.ledger.block(target.key,blocking_state,message)
            self.log.emit(f'[停止该账号] {target.key}：{message}；其他账号继续')
        elif account_block and account_block.get('status') in BLOCKING:
            self.blocked[target.key]=account_block
            self.log.emit(f'[停止该账号] {target.key}：{account_block["message"]}；其他账号继续')
        self.save_report()
        return blocking_state or state

    def archive(self, job):
        item = job['item']
        required=job['plan']['required']
        for key in required:
            latest=self.ledger.result(job['digest'],key)
            if latest:
                if latest.get('status') in (SUCCESS,'IGNORED','PERMANENT_REJECT','DURATION_LIMIT') or latest.get('task_id')==self.report['task_id']:
                    item['results'][key].update(latest)
                    if latest.get('status') in (SUCCESS,'IGNORED','PERMANENT_REJECT','DURATION_LIMIT'):
                        item['results'][key].pop('quota_message',None)
                        item['results'][key].pop('blocking_message',None)
        if (self.stopping.is_set() or item['archived_path'] or
            not required or not all(item['results'][key]['status'] in (SUCCESS,'IGNORED') for key in required) or
            not all(job['plan']['archive_enabled'].values())):
            return
        source = job['path']
        try:
            destination=archive_completed_file(source,job['signature'],self.log.emit,cloud_root=self.root)
            item['archived_path'] = str(destination)
            self.log.emit(f'[统一归档] {source.name}：本任务所选{len(required)}个账号已成功或不再跟踪；保留各账号原状态')
        except (OSError,ValueError) as error:
            item['archive_error'] = str(error)
        self.save_report()
        self.queue_changed.emit()

    def blocked_summary(self, *, include_reason=False):
        entries=[]
        for target in self.targets:
            block=self.blocked.get(target.key) or self.ledger.quota_block(target.key)
            if not block:
                continue
            text=f'{CHANNELS[target.platform].name}/{target.account.account_name}：{result_status_text(block)}，本轮停止该账号'
            if include_reason:
                text+='（'+' '.join(block.get('message','').split())[:220]+'）'
            entries.append(text)
        return '；'.join(entries)

    def cleanup_completed_copies(self):
        if self.stopping.is_set():
            return
        from .scan_cleanup import cleanup_completed_cloud_copies
        self.log.emit('[部署后检查] 核对运行期间新增的云盘副本；仅归档本轮所选账号均已成功的文件')
        try:
            items = cleanup_completed_cloud_copies(self.targets,self.root,
                history_targets=self.history_targets,
                exclude_paths=[item['file_path'] for item in self.report['files']],
                stopped=self.stopping.is_set,log=self.log.emit)
            records = {item['file_path']:item for item in self.report.get('completion_cleanup',[])}
            for item in items:
                records[item['file_path']] = item
                self.result_changed.emit(item)
            self.report['completion_cleanup'] = list(records.values())
            self.save_report()
            if items:
                self.queue_changed.emit()
        except Exception as error:
            self.report['completion_cleanup_error'] = str(error)
            self.log.emit(f'[部署后检查未完成] {error}；发布结果保留，未重新上传')

    def run_batch(self, jobs):
        # Copies in different bound directories share a content job. Finish all
        # selected accounts and their one retry before advancing to next content.
        groups={}
        for job in jobs:
            groups.setdefault(job['digest'],[]).append(job)
        for copies in groups.values():
            if not self.checkpoint():
                break
            from .drive_ingest import cloud_archive_scope
            scope = {t.key:t for copy in copies for t in cloud_archive_scope(
                self.root,copy['path'],copy['digest'],self.archive_targets)}
            plan = self.ledger.register_plan(copies[0]['digest'],list(scope.values()),fixed=True)
            for copy in copies:
                copy['plan'] = plan
                copy['item']['task_targets'] = list(scope)
            retries=[]
            self.log.emit(f'[文件开始] {copies[0]["path"].name}；平台顺序：'+
                ' → '.join(CHANNELS[t.platform].name+'/'+t.account.account_name for t in self.targets))
            for target in self.targets:
                if target.key not in scope:
                    continue  # A different cloud series is an independent resource binding.
                job=next((copy for copy in copies if target.key in {t.key for t in copy['targets']}),None)
                if job is None:
                    if self.ledger.result(copies[0]['digest'],target.key).get('status') not in (SUCCESS,'IGNORED'):
                        self.log.emit(f'[目录未包含此文件] {target.key}：未在该账号绑定目录找到同内容媒体；保留文件等待该账号完成')
                    continue  # Respect each account's bound directory/media filter.
                before = len(job['item']['results'][target.key]['attempts'])
                state = self.attempt(job, target)
                if state in RETRYABLE:
                    retries.append((job, target))
                if self.stopping.is_set():
                    break
                if len(job['item']['results'][target.key]['attempts']) > before and state not in BLOCKING | {'DURATION_LIMIT','PERMANENT_REJECT','FILE_CHANGED'}:
                    self.controlled_wait(self.interval_seconds)
            for job,target in retries:
                if not self.checkpoint():
                    break
                self.log.emit(f'[当前文件重试一次] {job["path"].name} → {target.key}')
                self.attempt(job,target)
                self.controlled_wait(self.interval_seconds)
            for job in copies:
                self.archive(job)
        if jobs and (summary:=self.blocked_summary(include_reason=True)):
            self.log.emit('[本轮账号限制] '+summary)

    def run(self):
        try:
            self.ledger = MultiTargetLedger(self.root)
            self.quota.bootstrap_history(self.stopping.is_set)
            self.save_report()
            self.log.emit(f'[任务报告] {self.report_path}')
            while not self.stopping.is_set():
                if not self.checkpoint():
                    break
                self.run_batch(self.plan_batch())
                self.cleanup_completed_copies()
                selected={t.key for t in self.targets}
                quota_wait=any(r.get('quota_message') and r.get('status') not in (SUCCESS,'IGNORED','PERMANENT_REJECT','DURATION_LIMIT')
                    for f in self.report['files'] for key,r in f['results'].items() if key in selected)
                if self.file_paths is not None and not quota_wait:
                    self.report['status'] = 'STOPPED' if self.stopping.is_set() else 'COMPLETED'
                    break
                if all(t.key in self.blocked or self.ledger.quota_block(t.key) for t in self.targets):
                    self.report['status'] = 'BLOCKED'
                    break
                summary=self.blocked_summary()
                interval=f'{self.poll_seconds:g}秒'
                next_scan=(datetime.now(CHINA_TIME)+timedelta(seconds=self.poll_seconds)).strftime('%Y-%m-%d %H:%M:%S')
                self.report['status']='WAITING_QUOTA' if quota_wait else 'RUNNING'
                self.status.emit(f'每日限额等待；提高对应账号上限或次日继续' if quota_wait else f'定时扫描等待（每{interval}）；'+(summary or '成功不重传；本任务失败最多尝试两次'))
                self.log.emit(f'[定时扫描等待] 本轮结束；每{interval}检查目录；下次 {next_scan}（北京时间）；成功不重传，失败2不再重试；限额账号等待跨日或提高上限')
                self.save_report()
                current=datetime.now(CHINA_TIME)
                until_midnight=((current+timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0)-current).total_seconds()
                self.controlled_wait(min(self.poll_seconds,until_midnight) if quota_wait else self.poll_seconds)
                if not self.stopping.is_set():
                    self.log.emit('[定时扫描开始] 正在检查本地媒体目录的新文件；这是目录扫描，不是平台投稿结果查询')
        except Exception as error:
            self.report.update(status='FAILED', error=str(error))
            self.log.emit(f'[任务异常] {error}')
        finally:
            if self.report['status'] in ('RUNNING', 'PAUSED', 'WAITING_QUOTA'):
                self.report['status'] = 'STOPPED'
            self.report['finished_at'] = now()
            for file in self.report['files']:
                for result in file['results'].values():
                    if result['status'] == 'PENDING':
                        result.update(status='CANCELLED', message='本次任务结束前尚未执行')
                    elif result['status'] in ('IN_PROGRESS','RETRYING','UNCONFIRMED'):
                        result.update(status='FAILED'+str(min(2,max(1,len(result.get('attempts',[]))))), message='任务中断，未确认成功；保留原文件')
            try:
                self.save_report()
                self.log.emit(f'[任务结束] 成功{self.report["success_count"]}，失败{self.report["failure_count"]}，未确认{self.report["unconfirmed_count"]}；{self.report_path}')
            except Exception as error:
                self.log.emit(f'[报告保存失败] {error}')
