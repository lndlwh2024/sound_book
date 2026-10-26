"""Serial multi-target publishing with persistent per-target results and deferred archive."""
import hashlib
import json
import threading
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone, timedelta
from pathlib import Path

from PySide6.QtCore import QThread, Signal
from .platforms import ROOT, CHANNELS, PUBLISH_ORDER
from .douyin.daemon import is_file_fully_written
from .media import selected_extensions, media_type
from .douyin.uploader import DouyinUploader, compute_file_md5
from .youtube import YouTubeUploader
from .creator_studio import XiaohongshuUploader, KuaishouUploader
from .wechat_channels import WeChatChannelsUploader
from .status import BLOCKING, RETRYABLE, STATUS_TEXT, result_status_text, fresh_result
from .upload_quota import DailyUploadQuota, DailyQuotaReached
from .event_log import redact
from .archive import archive_completed_file

CHINA_TIME = timezone(timedelta(hours=8))
SUCCESS = 'SUCCESS'
LOCAL_UPLOAD_INTERVAL_SECONDS = 5
LEGACY_REVIEW_MESSAGE = '发现原抖音成功账本；旧记录缺少本次确认依据，请核对后标记成功或允许重试，避免重复发布'
LEGACY_FAILED_MESSAGE = '旧未确认投稿按失败处理；新任务允许重试'
STUDIO_ADAPTERS = {'xiaohongshu': XiaohongshuUploader, 'kuaishou': KuaishouUploader,
                   'wechat_channels': WeChatChannelsUploader}


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
        if success:
            message='已确认抖音发布成功，等待全部必需目标完成后统一归档'
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
    active_file_changed = Signal(str)
    event_logged = Signal(dict)
    queue_changed = Signal()
    status = Signal(str)
    result_changed = Signal(dict)
    nickname_changed = Signal(dict)
    account_state_changed = Signal(dict)

    def __init__(self, targets, root=None, parent=None, uploader_factory=None, poll_seconds=LOCAL_UPLOAD_INTERVAL_SECONDS, interval_seconds=LOCAL_UPLOAD_INTERVAL_SECONDS, file_paths=None, archive_targets=None, deferred_preflight=None, history_targets=None, strict_recovery=False, task_id=None, initial_blocks=None):
        super().__init__(parent)
        self.targets = [PublishTarget(t.platform, replace(t.account), t.profile)
            for t in sorted(targets,key=lambda t:(PUBLISH_ORDER.index(t.platform),t.account.account_id))]
        self.root = Path(root) if root else ROOT / 'data' / 'multi_platform'
        self.strict_recovery = strict_recovery
        from .event_log import event_log
        self.events = event_log(self.root)
        self.log_context = {}
        self.reconciled = set()
        self.limit_notices = set()
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
        self._next_upload_at = 0
        self.stopping = threading.Event()
        self.pause_requested = threading.Event()
        self.wake_requested = threading.Event()
        self.initial_blocks = {key:dict(value) for key,value in (initial_blocks or {}).items()
                               if key in {target.key for target in self.targets}}
        self.blocked = {key:dict(value) for key,value in self.initial_blocks.items()}
        self.target_enabled = {t.key: t.account.enabled for t in self.targets}
        self.processed = set()
        self.file_paths = None if file_paths is None else {str(Path(p).resolve()) for p in file_paths}
        self.report = {'task_id': task_id or uuid.uuid4().hex, 'started_at': now(), 'status': 'RUNNING',
                       'task_type':'MANUAL' if self.file_paths is not None else 'CONTINUOUS',
                       'selected_files':sorted(self.file_paths) if self.file_paths is not None else None,
                       'files': [], 'targets': [{'key': t.key, 'platform': t.platform,
                        'account_name': t.account.account_name, 'directory': t.account.target_dir} for t in self.targets]}
        self.report_path = self.root / 'publish_reports' / ('publish_task_' + datetime.now(CHINA_TIME).strftime('%Y%m%d_%H%M%S') + '_' + self.report['task_id'][:8] + '.json')
        from .failure_diagnostics import TaskFailureDiagnostics
        self.diagnostics = TaskFailureDiagnostics(self.root, self.report['task_id'], self.events.task_events(self.report['task_id']))
        self.diagnostic_states = set()

    def emit_log(self,message,**context):
        self.log.emit(message)
        merged={**self.log_context,**context}
        event=self.events.write(message,task_id=self.report['task_id'],**merged)
        try:
            self.diagnostics.record_event(event)
        except OSError as error:
            self.log.emit('[失败诊断日志保存异常] '+str(error))
        self.event_logged.emit(event)

    def emit_account_state(self,target,state,message,source='upload'):
        from .account_batch_session import current_account_session
        session=current_account_session()
        if session:
            session.mark_auth(state)
        try:
            from .auth_snapshots import record_auth_result
            record_auth_result(self.root,target,state,self.emit_log,source=source,
                context=session.context if session else None,page=session.page if session else None,
                live=bool(session and session.context is not None) or source=='browser_resource',task_id=self.report['task_id'])
        except Exception as error:
            self.emit_log('[登录证据记录未完成] '+type(error).__name__+'；发布结果保留')
        self.account_state_changed.emit(dict(platform=target.platform,account_id=target.account.account_id,
            status=state,message=message,source=source,checked_at=now()))

    def _batch_closed(self,session):
        try:
            from .auth_snapshots import record_auth_result
            record_auth_result(self.root,session.target,'SUCCESS',self.emit_log,
                source='upload',live=False,task_id=self.report['task_id'])
        except Exception as error:
            self.emit_log('[登录证据记录未完成] '+type(error).__name__+'；发布结果保留')

    def apply_preflight_block(self,job,target,previous,attempts,used):
        """An unavailable account must not open a browser to reconcile old work."""
        block=self.initial_blocks[target.key]
        item=job['item']['results'][target.key]
        item['blocking_message']=block['message']
        retained_info=previous.get('info') or item.get('info')
        requires_review=bool(previous.get('requires_review') or item.get('requires_review')
                             or isinstance(retained_info,dict) and retained_info.get('submitted'))
        if requires_review:
            item.update(previous)
            item.update(status='RECOVERY_REQUIRED',blocking_message=block['message'],attempts=attempts,
                        requires_review=True,info=retained_info)
            if not previous.get('requires_review'):
                item.update(self.ledger.set_result(job['digest'],target.key,'RECOVERY_REQUIRED',
                    previous.get('message') or '先前已提交，结果未确认；保留记录等待核验',retained_info,
                    requires_review=True,attempt_count=used,
                    task_id=previous.get('task_id') or self.report['task_id']))
                item['blocking_message']=block['message']
            state='RECOVERY_REQUIRED'
        elif previous.get('status')=='FAILED2' or used>=2:
            item.update(previous)
            item.update(status='FAILED2',attempt_count=max(2,used),attempts=attempts,
                        blocking_message=block['message'])
            state='FAILED2'
        else:
            item.update(self.ledger.set_result(job['digest'],target.key,block['status'],block['message'],
                retained_info,attempt_count=used,requires_review=requires_review,task_id=self.report['task_id']))
            state=block['status']
        self.failure_operation.not_started_reason='该账号部署前检查未通过，本文件尚未上传：'+block['message']
        notice=(job['digest'],target.key,'initial_block')
        if notice not in self.limit_notices:
            self.emit_log('[账号暂停] '+self.failure_operation.not_started_reason+'；其他账号继续',level='ERROR')
            self.limit_notices.add(notice)
        self.save_report()
        return state

    def stop(self):
        self.stopping.set()
        self.wake_requested.set()
        self.pause_requested.clear()
        self.status.emit('正在停止当前任务；未确认成功的上传记录为失败，保留原文件')

    def pause_all(self):
        if self.stopping.is_set():
            return
        self.pause_requested.set()
        self.emit_log('[全平台暂停] 已请求暂停；当前文件完成后暂停，不启动后续文件和重试；浏览器上传与发布确认继续')
        self.status.emit('已请求暂停全部平台；等待当前文件结束后进入暂停，可点击继续或停止')

    def resume(self):
        self.pause_requested.clear()
        self.emit_log('[全平台继续] 继续同一任务及报告，沿用已有发布结果')
        self.status.emit('全部平台已继续部署')

    def checkpoint(self):
        """Pause at file boundaries, leaving active browser transfers / confirmation intact."""
        announced = False
        while self.pause_requested.is_set() and not self.stopping.is_set():
            if not announced:
                self.report['status'] = 'PAUSED'
                self.save_report()
                self.status.emit('全部平台已暂停；不会开始后续文件或重试，可继续当前任务')
                self.emit_log('[全平台暂停] 已进入暂停；保留任务与已有结果')
                announced = True
            self._idle_wait(.1)
        if announced and not self.stopping.is_set():
            self.report['status'] = 'RUNNING'
            self.save_report()
        return not self.stopping.is_set()

    def controlled_wait(self, seconds):
        remaining = seconds
        while remaining > 0 and self.checkpoint():
            started = time.monotonic()
            if self.wake_requested.is_set():
                self.wake_requested.clear()
                break
            self._idle_wait(min(.1, remaining))
            remaining -= time.monotonic() - started

    def wait_for_next_upload(self):
        """Wait only before real work; file notifications belong to the local check."""
        while self.checkpoint():
            remaining = self._next_upload_at - time.monotonic()
            if remaining <= 0:
                return True
            self._idle_wait(min(.1, remaining))
        return False

    def _idle_wait(self,seconds):
        from .account_batch_session import current_account_session
        session=current_account_session()
        if not session or not session.pump(max(1,int(seconds*1000))):
            self.stopping.wait(seconds)

    def make_uploader(self, target, log):
        if target.platform == 'douyin':
            # Existing browser operations unchanged; archive is owned by this task.
            return DeferredDouyinUploader(replace(target.account, auto_archive=False), stop_requested=self.stopping.is_set,
                                 progress_callback=log)
        if target.platform == 'youtube':
            return YouTubeUploader(target.account, target.profile, self.root / 'dom_reports',
                                   self.stopping.is_set, log)
        if target.platform in STUDIO_ADAPTERS:
            adapter=STUDIO_ADAPTERS[target.platform]
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
        self.report['unconfirmed_count'] = sum(r['status']=='RECOVERY_REQUIRED' for r in entries)
        self.report['ignored_count'] = sum(r['status']=='IGNORED' for r in entries)
        self.report['cancelled_count'] = sum(r['status'] == 'CANCELLED' for r in entries)
        failed = lambda item:any(r['status'] in {'FAILED','FAILED1','FAILED2'} for key,r in item['results'].items() if key in keys_for(item))
        summary = lambda item:{key:item[key] for key in ('file_name','file_path','size_bytes','modified_at','results','archived_path')}
        self.report['success_files'] = [summary(f) for f in self.report['files']
            if all(f['results'].get(key,{}).get('status') in (SUCCESS,'IGNORED') for key in keys_for(f))]
        self.report['failure_files'] = [summary(f) for f in self.report['files'] if failed(f)]
        self.report['source_success_count'] = len(self.report['success_files'])
        self.report['all_selected_published']=bool(self.report['files']) and len(self.report['success_files'])==len(self.report['files'])
        self.report['archive_pending_count']=sum(not f.get('archived_path') for f in self.report['success_files'])
        self.report['all_selected_completed']=self.report['all_selected_published'] and self.report['archive_pending_count']==0
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
        from .manual_hold import discover_manual_holds
        discover_manual_holds(self.root,self.targets,stopped=self.stopping.is_set,log=self.emit_log)
        self.log_context={}
        files = {}
        for target in self.targets:
            directory = Path(target.account.target_dir)
            if not target.account.target_dir or not directory.is_dir():
                self.emit_log(f'[目录无效] {target.key}：{directory}')
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
                self.emit_log(f'[文件暂不能读取] {job["path"].name}：{error}；下次扫描再处理')
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
                        self.emit_log(f'[历史成功迁移] {job["path"].name} → {target.key}：归档目标的旧成功记录已恢复，未重新上传')
                if previous:
                    restored[target.key]=fresh_result(previous)
            except RuntimeError as error:
                self.blocked[target.key]={'status':'CONFIG_REQUIRED','message':str(error)}
                self.emit_log(f'[历史核对失败] {target.key}：{error}；其他上传账号继续，保留文件')
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
            self.emit_log(f'[历史成功恢复] {job["path"].name} → {target.key}；旧成功记录匹配，未重新投稿')
        return result

    def attempt(self, job, target):
        operation = self.diagnostics.operation(target.platform, target.account.account_id, job['path'].name)
        operation.auth_target, operation.auth_log = target, self.emit_log
        self.failure_operation = operation
        previous_attempts = len(job['item']['results'][target.key]['attempts'])
        try:
            status = self._attempt(job,target)
            return status
        finally:
            result = job['item']['results'].get(target.key, {})
            state = locals().get('status', 'FAILED')
            marker = (job['digest'], target.key, state)
            if state != SUCCESS and (operation.captures or marker not in self.diagnostic_states):
                try:
                    message = (result.get('quota_message', '') if state == 'DAILY_LIMIT'
                               else result.get('message', ''))
                    folder = operation.complete(state, message, result=result)
                    self.diagnostic_states.add(marker)
                    self.diagnostic_states.add((job['digest'], target.key, result.get('status', state)))
                    evidence = operation.capture_summary()
                    details = ('；采集异常='+'；'.join(evidence['errors'])) if evidence['errors'] else ''
                    self.emit_log(f'[失败诊断归档] {STATUS_TEXT.get(state,state)}（{state}）；原因={message}；'
                        f'保存目录={folder}；DOM文件={evidence["dom_files"]}；'
                        f'截图={evidence["screenshots"]}{details}', platform=target.platform)
                except OSError as error:
                    self.emit_log('[失败诊断保存异常] '+str(error), platform=target.platform)
            self.active_file_changed.emit('')
            attempts = job['item']['results'][target.key]['attempts']
            if len(attempts) > previous_attempts:
                # Browser work and task diagnostics have finished. Keep a full,
                # separate idle gap; a new-file notification cannot skip it.
                self._next_upload_at = time.monotonic() + self.interval_seconds

    def _attempt(self, job, target):
        self.log_context={'platform':target.platform,'account_id':target.account.account_id,
            'account_name':target.account.account_name,'file_name':job['path'].name,'sha256':job['digest']}
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
        held=self.find_manual_hold(job,target)
        if held:
            return self.apply_manual_hold(job,target,previous,held)
        attempts = self.attempts_by_target.setdefault((job['digest'],target.key),[])
        item['attempts'] = attempts
        # These adapters can validate captured acknowledgements entirely
        # locally. This is allowed even at the retry ceiling; no browser opens.
        if previous.get('info') and target.platform in ('douyin', *STUDIO_ADAPTERS):
            if target.platform=='douyin':
                validator=DeferredDouyinUploader(replace(target.account,auto_archive=False))
            else:
                adapter=STUDIO_ADAPTERS[target.platform]
                validator=adapter(target.account,target.profile,self.root/'dom_reports')
            if validator.reconcile(previous['info']) is True:
                self.quota.record_success(target.key,job['digest'],previous.get('task_id',''),previous.get('updated_at'))
                item.update(self.ledger.set_result(job['digest'],target.key,SUCCESS,
                    '已核验本地保存的平台成功回执，未打开浏览器或重新上传',previous['info']))
                item.pop('quota_message',None)
                self.save_report()
                return SUCCESS
        used = max(int(previous.get('attempt_count') or 0), max((a['number'] for a in attempts),default=0))
        if target.key in self.initial_blocks:
            if target.platform=='douyin' and legacy_import_needed(previous):
                try:
                    historical=self.seed_douyin_success(job,target)
                except (OSError,RuntimeError) as error:
                    self.emit_log('[历史成功暂未恢复] '+str(error)+'；账号仍暂停，不打开浏览器')
                    historical=None
                if historical:
                    item.update(historical)
                    self.save_report()
                    return SUCCESS
            return self.apply_preflight_block(job,target,previous,attempts,used)
        if previous.get('status')=='FAILED2' or used>=2:
            item.update(status='FAILED2',attempt_count=max(2,used),message='已达到两次尝试上限；自动扫描和重启均不再上传或打开核对浏览器，需手动重试')
            marker=(job['digest'],target.key)
            if marker not in self.limit_notices:
                self.emit_log('[重试已封顶] '+item['message'])
                self.limit_notices.add(marker)
            self.save_report()
            return 'FAILED2'
        if previous.get('requires_review'):
            item.update(previous)
            item.update(status='RECOVERY_REQUIRED', message='上次上传或提交中断；请先在平台核验，再使用“手动上传所选媒体”确认重试')
            self.save_report()
            return 'RECOVERY_REQUIRED'
        self.active_file_changed.emit(str(job['path']))
        marker=(job['digest'],target.key)
        if (previous.get('info') and (previous.get('status') in ('UNCONFIRMED','IN_PROGRESS','FAILED','FAILED1')
                or previous.get('manual_retry') and previous['info'].get('submitted'))
                and marker not in self.reconciled and not (target.platform=='youtube' and previous['info'].get('draft') is True)):
            self.reconciled.add(marker)
            try:
                self.emit_log('[历史结果核验] 本任务仅核对一次已知结果，不选择文件、不新建投稿')
                checker=self.factory(target,self.emit_log)
                checker.failure_capture = self.failure_operation.capture
                reconcile=getattr(checker,'reconcile',None)
                if callable(reconcile) and reconcile(previous['info']) is True:
                    self.quota.record_success(target.key,job['digest'],previous.get('task_id',''),previous.get('updated_at'))
                    item.update(self.ledger.set_result(job['digest'],target.key,SUCCESS,'自动检查确认先前投稿成功，未重新上传',previous['info']))
                    item.pop('quota_message',None)
                    item.pop('blocking_message',None)
                    self.save_report()
                    return SUCCESS
            except Exception as error:
                self.emit_log(f'[自动检查] {target.key}：{error}；未确认成功，按失败重试规则处理')
            if target.platform == 'youtube' and previous.get('manual_retry') and previous['info'].get('submitted'):
                message = '已有提交记录仍未确认；本次只读核验原视频ID，未重新上传或再次发布，请在平台核验'
                item.update(self.ledger.set_result(job['digest'], target.key, 'RECOVERY_REQUIRED', message,
                    previous['info'], requires_review=True, attempt_count=used, task_id=self.report['task_id']))
                self.emit_log('[已提交视频核验] '+message)
                self.save_report()
                return 'RECOVERY_REQUIRED'
        if len(attempts)>=2:
            item.update(status='FAILED2',attempt_count=2,message=previous.get('message','本任务已尝试两次，不再重试'))
            self.save_report()
            return 'FAILED2'
        block=self.blocked.get(target.key) or self.ledger.quota_block(target.key)
        if block:
            if attempts and item['status'] in ('FAILED1','FAILED2'):
                item['blocking_message']=block['message']
            else:
                item.update(self.ledger.set_result(job['digest'],target.key,block['status'],block['message'],
                    previous.get('info'),attempt_count=used,requires_review=False,
                    task_id=self.report['task_id'],blocked_by={k:block[k] for k in ('source_file','first_seen') if k in block}))
            source='（来源文件：'+block['source_file']+'）' if block.get('source_file') else ''
            self.failure_operation.not_started_reason=(f'本文件因账号{STATUS_TEXT.get(block["status"],block["status"])}暂停，'
                '尚未上传文件；沿用本轮账号检查结果'+source+'：'+block['message'])
            notice=(job['digest'],target.key,'blocked',block['status'])
            if notice not in self.limit_notices:
                self.emit_log('[本文件暂停] '+self.failure_operation.not_started_reason,level='ERROR')
                self.limit_notices.add(notice)
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
        if target.platform in ('youtube', *STUDIO_ADAPTERS) and media_type(path)!='video':
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
            item['quota_message']=f'今日已成功发布{quota["used"]}个，上限{quota["limit"]}个；北京时间次日00:00或提高本账号上限后继续'
            marker=(quota['day'],quota['limit'],quota['used'])
            if self.quota_notices.get(target.key)!=marker:
                self.emit_log(f'[每日限额等待] {target.key}：'+item['quota_message'])
                self.quota_notices[target.key]=marker
            self.save_report()
            return 'DAILY_LIMIT'
        item.pop('quota_message',None)
        if not self.wait_for_next_upload():
            item.update(status='CANCELLED',message='用户停止，未开始下一次上传')
            self.save_report()
            return 'CANCELLED'
        if target.key in self.deferred_preflight:
            from .preflight import AccountPreflight, prerequisite_results
            from playwright.sync_api import sync_playwright
            failures=prerequisite_results([target],self.root/'dom_reports')
            if not failures:
                from .account_batch_session import account_playwright
                with account_playwright(sync_playwright) as playwright:
                    check = AccountPreflight(target,self.stopping.is_set,self.emit_log)
                    check.failure_capture = self.failure_operation.capture
                    result=check.check(playwright)
                failures=[] if result.passed else [result]
                if result.passed and result.identity:
                    self.nickname_changed.emit(dict(result.identity,platform=target.platform,account_id=target.account.account_id))
            if failures:
                failure=failures[0]
                self.blocked[target.key]={'status':failure.status,'message':failure.message}
                self.emit_account_state(target,failure.status,failure.message,'preflight')
                item.update(status=failure.status,message=failure.message)
                self.save_report()
                return failure.status
            self.deferred_preflight.remove(target.key)
            self.emit_account_state(target,'SUCCESS','已核对当前登录和发布入口','preflight')
        attempt={'number':used+1,'id':self.failure_operation.operation_id,'started_at':now(),'logs':[],'upload_started':False}
        attempts.append(attempt)
        self.ledger.set_result(job['digest'],target.key,'IN_PROGRESS',f'开始第{attempt["number"]}/2次尝试',
            previous.get('info'),task_id=self.report['task_id'],attempt_count=attempt['number'])
        self.emit_log(f'[上传流程开始] 第{attempt["number"]}/2次尝试；文件={path.name}；本地路径={path}；大小={job["signature"][0]}字节')
        item['status']='RETRYING' if attempt['number']==2 else 'IN_PROGRESS'
        self.save_report()
        def log(message):
            message=redact(message)
            attempt['logs'].append({'time':now(),'message':message})
            # Complete stage logs remain in JSONL and the task report.
            self.emit_log(f'[{target.platform}/{target.account.account_name}] {message}')
            self.save_report()
        quota_error=None
        manual_hold_error=None
        boundary_info=None
        def before_upload():
            nonlocal quota_error,manual_hold_error
            if self.stopping.is_set():
                raise RuntimeError('用户停止，尚未开始上传')
            manual_hold_error=self.find_manual_hold(job,target)
            if manual_hold_error:
                raise RuntimeError('文件已被人工移入publiced，暂停此文件；其他文件继续')
            try:
                attempt['upload_started_at']=self.quota.reserve(target.key,job['digest'],self.report['task_id'],attempt['id'],getattr(target.account,'daily_upload_limit',10))
            except DailyQuotaReached as error:
                quota_error=error
                raise
            attempt['upload_started']=True
            self.ledger.set_result(job['digest'], target.key, 'IN_PROGRESS',
                '文件传输已开始；尚未确认发布成功',previous.get('info'),
                requires_review=getattr(uploader,'supports_upload_boundary',False) is True,
                attempt_count=attempt['number'],task_id=self.report['task_id'])
            usage=self.quota.snapshot(target.key,getattr(target.account,'daily_upload_limit',10))
            self.emit_log(f'[发布额度] {target.platform}/{target.account.account_name}：今日已成功发布{usage["used"]}个，上限{usage["limit"]}个，剩余{usage["remaining"]}个；开始上传不计数')
            self.save_report()
        def before_submit(info):
            nonlocal boundary_info
            boundary_info=info
            # These adapters validate saved evidence locally. Do not invoke the
            # YouTube browser reconciliation inside its active upload context.
            confirmed=(target.platform in STUDIO_ADAPTERS
                and callable(getattr(uploader,'reconcile',None)) and uploader.reconcile(info) is True)
            state=SUCCESS if confirmed else 'IN_PROGRESS'
            message='平台已确认本次发布成功，回执已保存' if confirmed else '已提交，等待成功证据；中断后按失败处理'
            saved=self.ledger.set_result(job['digest'],target.key,state,message,info,
                task_id=self.report['task_id'],attempt_count=attempt['number'],
                requires_review=not confirmed)
            if confirmed:
                self.quota.record_success(target.key,job['digest'],self.report['task_id'])
                item.update(saved)
            else:
                item.update(message='已提交，等待平台确认',info=info)
            self.save_report()
        self.status.emit(f'{target.platform}/{target.account.account_name}：{path.name}')
        uploader = None
        try:
            uploader=self.factory(target,log)
            uploader.nickname_changed=lambda identity:self.nickname_changed.emit(
                dict(identity,platform=target.platform,account_id=target.account.account_id))
            uploader.failure_capture = self.failure_operation.capture
            uploader.before_upload=before_upload
            uploader.before_submit=before_submit
            if target.platform=='youtube':
                uploader.resume_info=previous.get('info')
                def draft_checkpoint(info):
                    attempt['info']=info
                    self.ledger.set_result(job['digest'],target.key,'IN_PROGRESS',
                        '已记录YouTube草稿ID；后续重试继续该草稿，不重新选择文件',info,
                        requires_review=False,attempt_count=attempt['number'],task_id=self.report['task_id'])
                    self.emit_log('[草稿已记录] 视频ID='+info['video_id']+'；未发布；优先恢复此草稿')
                    self.save_report()
                uploader.on_draft_identified=draft_checkpoint
            # Injected adapters also check allowance before transfer, without spending it.
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
        if manual_hold_error:
            attempts.pop()
            return self.apply_manual_hold(job,target,previous,manual_hold_error)
        if quota_error:
            attempts.pop()
            self.ledger.set_result(job['digest'],target.key,previous.get('status','PENDING'),
                '额度竞争导致尚未开始传输；未消耗本次尝试',previous.get('info'),attempt_count=used,
                requires_review=previous.get('requires_review',False))
            item.update(status='FAILED1' if attempts else 'PENDING',quota_message=str(quota_error))
            self.save_report()
            return 'DAILY_LIMIT'
        # Account authentication and the uncertainty of an already-submitted
        # file are separate results. Adapters may retain FAILED for the file
        # while observing a definite login rejection before page cleanup.
        observed_block=getattr(uploader,'account_block',None) if uploader is not None else None
        if state!=SUCCESS and isinstance(observed_block,dict) and observed_block.get('status') in BLOCKING:
            info=dict(info) if isinstance(info,dict) else {}
            info['account_block']=dict(observed_block)
        message=redact(message)
        saved=self.ledger.result(job['digest'],target.key)
        if (state!=SUCCESS and saved.get('status')==SUCCESS and saved.get('task_id')==self.report['task_id']
                and saved.get('attempt_count')==attempt['number']):
            state,message,info=SUCCESS,'已保存本次平台成功回执，页面中断不改变结果',saved.get('info')
        if self.stopping.is_set() and not attempt['upload_started']:
            state='CANCELLED'
        if state in ('UNCONFIRMED','FAILED') or state=='CANCELLED' and attempt['upload_started']:
            state='FAILED'+str(attempt['number'])
        blocking_state=state if state in BLOCKING else None
        if blocking_state:
            self.emit_account_state(target,blocking_state,message)
        elif state==SUCCESS:
            self.emit_account_state(target,'SUCCESS','已确认本次发布成功')
        if blocking_state and attempt['upload_started']:
            info=dict(info) if isinstance(info,dict) else {}
            info['account_block']={'status':blocking_state,'message':message}
            state='FAILED'+str(attempt['number'])
        consumed = attempt['number']
        if not attempt['upload_started'] and state in BLOCKING | {'CANCELLED','DURATION_LIMIT','PERMANENT_REJECT'}:
            attempts.pop()
            consumed = used
        retained_info = info or boundary_info or saved.get('info') or previous.get('info')
        unknown_youtube_transfer = (target.platform == 'youtube' and attempt['upload_started']
            and getattr(uploader,'supports_upload_boundary',False) is True
            and not (isinstance(retained_info,dict) and retained_info.get('video_id')))
        # An explicit current submission rejection is a known failure, not an
        # unknown acceptance. A challenge/closed page alone remains uncertain.
        rejected = (target.platform in STUDIO_ADAPTERS
            and getattr(uploader,'business_rejected',False) is True)
        confirmation = retained_info.get('confirmation') if isinstance(retained_info,dict) else None
        if target.platform == 'douyin' and isinstance(confirmation,dict) and confirmation.get('code') is not None:
            code = str(confirmation['code'])
            rejected = code != '0' and code.lstrip('-').isdigit()
        review = state != SUCCESS and not rejected and (boundary_info is not None or unknown_youtube_transfer)
        if review:
            state, message = 'RECOVERY_REQUIRED', '上传或提交结果未确认，请先核验平台，未自动重传；原始原因：' + message
        item.update(self.ledger.set_result(job['digest'],target.key,state,message,retained_info,
            task_id=self.report['task_id'],attempt_count=consumed, requires_review=review))
        attempt.update(status=state,finished_at=now(),message=message,info=retained_info)
        if state==SUCCESS:
            self.quota.record_success(target.key,job['digest'],self.report['task_id'])
            usage=self.quota.snapshot(target.key,getattr(target.account,'daily_upload_limit',10))
            self.emit_log(f'[成功发布计数] 今日已成功发布{usage["used"]}个，上限{usage["limit"]}个，剩余{usage["remaining"]}个')
        self.emit_log(f'[上传流程结束] 结果={state}；原因={message}；已用尝试={consumed}/2；'
            + ('已确认成功，等待归档' if state==SUCCESS else '保留原文件；未把草稿计为发布成功'),
            level='INFO' if state==SUCCESS else 'ERROR')
        account_block=(info or {}).get('account_block') if isinstance(info,dict) else None
        if blocking_state:
            self.blocked[target.key]={'status':blocking_state,'message':message,'source_file':path.name,'first_seen':now()}
            if blocking_state=='ACCOUNT_LIMIT':
                self.ledger.block(target.key,blocking_state,message)
            self.emit_log(f'[停止该账号] {target.key}：{message}；其他账号继续')
        elif account_block and account_block.get('status') in BLOCKING:
            self.blocked[target.key]=account_block
            self.emit_account_state(target,account_block['status'],account_block['message'])
            self.emit_log(f'[停止该账号] {target.key}：{account_block["message"]}；其他账号继续')
        self.save_report()
        return blocking_state or state

    def find_manual_hold(self,job,target):
        from .manual_hold import find_manual_hold
        return find_manual_hold(self.root,job['path'],job['digest'],target)

    def apply_manual_hold(self,job,target,previous,hold):
        message='文件已被人工移入publiced，等待人工确认；不会自动重传，也不记发布成功'
        info=previous.get('info')
        saved=self.ledger.set_result(job['digest'],target.key,'MANUAL_HOLD',message,info,
            attempt_count=int(previous.get('attempt_count') or 0),
            requires_review=bool(previous.get('requires_review')),manual_hold_id=hold.get('hold_id'),
            task_id=self.report['task_id'])
        job['item']['results'][target.key].update(saved)
        self.failure_operation.not_started_reason=message
        notice=(job['digest'],target.key,'manual_hold',hold.get('hold_id'))
        if notice not in self.limit_notices:
            self.limit_notices.add(notice)
            self.emit_log('[文件人工归档待确认] '+job['path'].name+'；'+message,level='INFO')
        self.save_report()
        return 'MANUAL_HOLD'

    def archive(self, job):
        self.log_context={'file_name':job['path'].name,'sha256':job['digest']}
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
            destination=archive_completed_file(source,job['signature'],self.emit_log,cloud_root=self.root,
                                               stopped=self.stopping.is_set)
            item['archived_path'] = str(destination)
            self.emit_log(f'[统一归档] {source.name}：本任务所选{len(required)}个账号已成功或不再跟踪；保留各账号原状态')
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
        self.emit_log('[部署后检查] 核对运行期间新增的云盘副本；仅归档本轮所选账号均已成功的文件')
        try:
            items = cleanup_completed_cloud_copies(self.targets,self.root,
                history_targets=self.history_targets,
                exclude_paths=[item['file_path'] for item in self.report['files']],
                stopped=self.stopping.is_set,log=self.emit_log)
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
            self.emit_log(f'[部署后检查未完成] {error}；发布结果保留，未重新上传')

    def run_batch(self, jobs):
        # Freeze this round: newly downloaded files join the next account pass.
        from .account_batch_session import AccountBatchSession,AccountBatchResourceError
        if getattr(self,'_browser_resource_error',None):
            return
        groups={}
        for job in jobs:
            groups.setdefault(job['digest'],[]).append(job)
        prepared=[]
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
            prepared.append((copies,scope))
        for target in self.targets:
            if not self.checkpoint():
                break
            account_jobs=[]
            for copies,scope in prepared:
                if target.key not in scope:
                    continue
                job=next((copy for copy in copies if target.key in {t.key for t in copy['targets']}),None)
                if job is not None:
                    account_jobs.append(job)
                elif self.ledger.result(copies[0]['digest'],target.key).get('status') not in (SUCCESS,'IGNORED'):
                    self.emit_log(f'[目录未包含此文件] {target.key}：未在该账号绑定目录找到同内容媒体；保留文件等待该账号完成')
            if not account_jobs:
                continue
            def account_log(message,target=target):
                self.emit_log(message,platform=target.platform,account_id=target.account.account_id,
                    account_name=target.account.account_name,file_name='',sha256='')
            self.emit_log(f'[账号批次开始] {CHANNELS[target.platform].name}/{target.account.account_name}；本批次{len(account_jobs)}个文件')
            try:
                with AccountBatchSession(target,account_log,self.stopping.is_set,self._batch_closed):
                    retries=[]
                    for job in account_jobs:
                        if not self.checkpoint():
                            break
                        self.emit_log(f'[文件开始] {job["path"].name} → {target.key}')
                        state=self.attempt(job,target)
                        if state in RETRYABLE:
                            retries.append(job)
                        self.archive(job)
                    for job in retries:
                        if not self.checkpoint():
                            break
                        self.emit_log(f'[本账号文件重试一次] {job["path"].name} → {target.key}')
                        self.attempt(job,target)
                        self.archive(job)
            except Exception as error:
                message=f'本账号浏览器会话未能正常结束：{type(error).__name__}：{error}'
                self.blocked[target.key]={'status':'FAILED','message':message}
                resource_failure=isinstance(error,AccountBatchResourceError)
                self.emit_account_state(target,'FAILED',message,'browser_resource' if resource_failure else 'upload')
                self.emit_log('[账号批次结束异常] '+message+'；保留文件及已有结果，'+
                              ('暂停后续浏览器操作' if resource_failure else '继续其他账号'))
                if resource_failure:
                    self._browser_resource_error=message
                    for remaining in self.targets:
                        self.blocked[remaining.key]={'status':'FAILED','message':'浏览器资源未释放，暂不打开新会话；'+message}
                        if remaining.key!=target.key:
                            self.emit_account_state(remaining,'FAILED',self.blocked[remaining.key]['message'],'browser_resource')
                    self.emit_log('[浏览器资源暂停] 后续账号不会打开浏览器；请停止任务并关闭残留窗口后重新启动')
                    break
            finally:
                self.emit_log('[账号批次结束] '+target.key)
        for copies,_ in prepared:
            for job in copies:self.archive(job)
        if jobs and (summary:=self.blocked_summary(include_reason=True)):
            self.emit_log('[本轮账号限制] '+summary)

    def run(self):
        try:
            self.ledger = MultiTargetLedger(self.root)
            self.quota.bootstrap_history(self.stopping.is_set)
            self.save_report()
            self.emit_log(f'[任务报告] {self.report_path}')
            while not self.stopping.is_set():
                if not self.checkpoint():
                    break
                self.run_batch(self.plan_batch())
                self.cleanup_completed_copies()
                selected={t.key for t in self.targets}
                quota_wait=any(r.get('quota_message') and r.get('status') not in (SUCCESS,'IGNORED','PERMANENT_REJECT','DURATION_LIMIT')
                    for f in self.report['files'] for key,r in f['results'].items() if key in selected)
                if self.file_paths is not None and (self.report['all_selected_completed'] or not quota_wait):
                    self.report['status'] = 'STOPPED' if self.stopping.is_set() else 'COMPLETED'
                    self.emit_log('[手工任务结束] '+('所选平台全部完成；' if self.report['all_selected_completed'] else '本轮尝试已结束；')+
                        '部署自动停止；成功项不重传，不转入目录轮询')
                    break
                if all(t.key in self.blocked or self.ledger.quota_block(t.key) for t in self.targets):
                    self.report['status'] = 'WAITING_ACCOUNTS'
                    self.status.emit('当前所选账号均已暂停；'+self.blocked_summary(include_reason=True))
                    self.save_report()
                    self.controlled_wait(self.poll_seconds)
                    continue
                summary=self.blocked_summary()
                interval=f'{self.poll_seconds:g}秒'
                next_scan=(datetime.now(CHINA_TIME)+timedelta(seconds=self.poll_seconds)).strftime('%Y-%m-%d %H:%M:%S')
                self.report['status']='WAITING_QUOTA' if quota_wait else 'RUNNING'
                self.status.emit(f'每日限额等待；提高对应账号上限或次日继续' if quota_wait else f'等待本地视频（每{interval}检查）；'+(summary or '成功不重传；失败2需手动重试'))
                self.emit_log(f'[等待本地视频] 每{interval}检查本地文件；下次 {next_scan}（北京时间）；网盘按公共轮询间隔独立检查；成功不重传；失败2需手动重试；限额账号等待跨日或提高上限',level='INFO')
                self.save_report()
                current=datetime.now(CHINA_TIME)
                until_midnight=((current+timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0)-current).total_seconds()
                self.controlled_wait(min(self.poll_seconds,until_midnight) if quota_wait else self.poll_seconds)
                if not self.stopping.is_set():
                    self.emit_log('[检查本地视频] 查找已下载完整且尚未发布的视频；不刷新网盘')
        except Exception as error:
            self.report.update(status='FAILED', error=str(error))
            self.emit_log(f'[任务异常] {error}')
        finally:
            if self.report['status'] in ('RUNNING', 'PAUSED', 'WAITING_QUOTA', 'WAITING_ACCOUNTS'):
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
                self.emit_log(f'[任务结束] 成功{self.report["success_count"]}，失败{self.report["failure_count"]}，未确认{self.report["unconfirmed_count"]}；{self.report_path}')
            except Exception as error:
                self.emit_log(f'[报告保存失败] {error}')
            try:
                # Include targets stopped before their browser could be started.
                for file in self.report['files']:
                    for target in self.targets:
                        if target.key not in file.get('task_targets', [t.key for t in self.targets]):
                            continue
                        result = file.get('results', {}).get(target.key, {})
                        state = result.get('status', 'CANCELLED')
                        marker = (file['sha256'], target.key, state)
                        if state != SUCCESS and marker not in self.diagnostic_states:
                            self.diagnostics.operation(target.platform, target.account.account_id, file['file_name']).complete(
                                state, result.get('message', ''), result=result)
                self.diagnostics.finish()
            except OSError as error:
                self.emit_log('[失败诊断整理异常] '+str(error))
