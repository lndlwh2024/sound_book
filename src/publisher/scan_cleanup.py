"""Cancellable content scan, history reconciliation and completed-file cleanup."""
import hashlib
import json
import threading
from pathlib import Path
from PySide6.QtCore import QThread, Signal
from .multi_deploy import MultiTargetLedger, archive_completed_file, CHINA_TIME, LEGACY_FAILED_MESSAGE, legacy_import_needed
from .douyin.daemon import is_file_fully_written
from .media import selected_extensions
from .status import fresh_result
from .upload_quota import DailyUploadQuota
from datetime import datetime


def scan_token(root, targets, archive_targets, history_targets=None):
    """Cheap metadata validation; only the background worker computes hashes."""
    listing=[]
    for target in targets:
        folder=Path(target.account.target_dir).resolve()
        if not target.account.target_dir or not folder.is_dir():
            raise ValueError(f'{target.account.account_name}的媒体目录不存在：{folder}')
        allowed=selected_extensions(target.account)
        if not allowed:
            raise ValueError(f'{target.account.account_name}未选择媒体类型')
        files=[]
        for path in folder.iterdir():
            try:
                if path.is_file() and path.suffix.lower() in allowed:
                    stat=path.stat()
                    files.append((str(path),stat.st_size,stat.st_mtime_ns))
            except OSError:
                files.append((str(path),None,None))
        listing.append((target.key,str(folder),tuple(sorted(allowed)),tuple(sorted(files))))
    ledger=Path(root)/'target_ledger.json'
    stamp=(ledger.stat().st_size,ledger.stat().st_mtime_ns) if ledger.exists() else None
    group=tuple(sorted((t.key,t.account.auto_archive) for t in archive_targets))
    historical=[]
    history_targets=history_targets if history_targets is not None else archive_targets
    accounts={t.key:t for t in [*history_targets,*targets] if t.platform=='douyin'}
    for key,target in sorted(accounts.items()):
        reports=Path(target.account.target_dir)/'reports'
        files=[]
        for report in reports.glob('publish_ledger_*.json'):
            stat=report.stat()
            files.append((str(report.resolve()),stat.st_size,stat.st_mtime_ns))
        historical.append((key,str(reports.resolve()),tuple(sorted(files))))
    return tuple(listing),group,stamp,tuple(historical),tuple(sorted(t.key for t in history_targets))


class ScanCleanupWorker(QThread):
    log=Signal(str)
    status=Signal(str)
    result_changed=Signal(dict)

    def __init__(self,targets,archive_targets,root,parent=None,cache=None,force=False,history_targets=None):
        super().__init__(parent)
        self.targets,self.archive_targets=targets,archive_targets
        self.history_targets=history_targets if history_targets is not None else archive_targets
        self.root=Path(root)
        self.cache=cache if cache is not None else {}
        self.force=force
        self.error=''
        self.cancelled=False
        self.stopping=threading.Event()
        self.items=[]

    def stop(self):
        self.stopping.set()
        self.requestInterruption()

    def check_stop(self):
        if self.stopping.is_set() or self.isInterruptionRequested():
            self.cancelled=True
            raise InterruptedError('扫描清理已取消，未启动部署')

    def import_reports(self,ledger):
        for path in sorted((self.root/'publish_reports').glob('*.json')):
            self.check_stop()
            DailyUploadQuota(self.root).bootstrap_history(lambda:self.stopping.is_set() or self.isInterruptionRequested())
            try:
                report=json.loads(path.read_text(encoding='utf-8-sig'))
                for item in report.get('files',[]):
                    digest=item.get('sha256','')
                    if len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest):
                        continue
                    for key,result in item.get('results',{}).items():
                        if result.get('status') in ('SUCCESS','IGNORED') and not ledger.result(digest,key):
                            ledger.set_result(digest,key,result['status'],'沿用历史任务的已确认结果',result.get('info'))
            except (OSError,ValueError,KeyError,TypeError) as error:
                raise ValueError(f'历史任务记录读取失败，停止清理以避免重复投稿：{path}：{error}') from error

    def legacy_success(self,ledger,path,digest,md5,target,signature):
        previous=ledger.restore_legacy_success(digest,target.key,path,signature)
        if not legacy_import_needed(previous) or target.platform!='douyin':
            return previous
        for report in (Path(target.account.target_dir)/'reports').glob('publish_ledger_*.json'):
            records=json.loads(report.read_text(encoding='utf-8-sig'))
            if not isinstance(records,list) or any(not isinstance(r,dict) for r in records):
                raise ValueError(f'原抖音账本结构无效：{report}')
            for record in records:
                if (record.get('account_id')==target.account.account_id and record.get('status')=='SUCCESS'
                        and record.get('file_hash')==md5):
                    return ledger.set_result(digest,target.key,'SUCCESS','MD5及账号匹配，迁移历史成功到SHA-256',record)
        return previous

    def run(self):
        try:
            self.check_stop()
            token=scan_token(self.root,self.targets,self.archive_targets,self.history_targets)
            cache_key=tuple(sorted(t.key for t in self.targets))
            if not self.force and len(self.targets)>1:
                cached=[self.cache.get((target.key,),{}) for target in self.targets]
                if all(entry.get('token')==scan_token(self.root,[target],self.archive_targets,self.history_targets)
                        for entry,target in zip(cached,self.targets)):
                    self.items=list({item['file_path']:item for entry in cached for item in entry['items']}.values())
                    for item in self.items:
                        self.result_changed.emit(item)
                    self.log.emit('[扫描清理] 所选账号均已完成有效扫描清理，直接进入部署检查')
                    return
            if not self.force and self.cache.get(cache_key,{}).get('token')==token:
                self.items=self.cache[cache_key]['items']
                for item in self.items:
                    self.result_changed.emit(item)
                self.log.emit('[扫描清理] 账号、目录内容、归档目标及历史结果均未变化，沿用已完成的扫描清理')
                return
            hashes={}
            cached=self.cache.get(cache_key,{})
            old_token=cached.get('token',())
            if (not self.force and len(old_token)==len(token)
                    and all(old_token[i]==token[i] for i in (0,2,3,4))):
                hashes={item['file_path']:item for item in cached.get('items',[]) if not item['archived_path']}
                self.log.emit('[扫描清理] 沿用已完成的扫描清理摘要；按本次所选账号重新核对归档范围')
            ledger=MultiTargetLedger(self.root)
            self.import_reports(ledger)
            files={}
            for target in self.targets:
                for path in Path(target.account.target_dir).resolve().iterdir():
                    if path.is_file() and path.suffix.lower() in selected_extensions(target.account):
                        files.setdefault(path,[]).append(target)
            archived=0
            cacheable=True
            for index,(path,targets) in enumerate(sorted(files.items()),1):
                self.check_stop()
                self.status.emit(f'扫描目录清理文件 {index}/{len(files)}：{path.name}')
                try:
                    before=path.stat()
                    signature=(before.st_size,before.st_mtime_ns)
                    if not is_file_fully_written(path):
                        cacheable=False
                        self.log.emit(f'[等待文件就绪] {path.name}；未清理、未上传')
                        continue
                    cached_item=hashes.get(str(path),{})
                    if (cached_item.get('size_bytes'),cached_item.get('mtime_ns'))==signature and cached_item.get('md5'):
                        digest,md5_digest=cached_item['sha256'],cached_item['md5']
                    else:
                        sha,md5=hashlib.sha256(),hashlib.md5()
                        with path.open('rb') as stream:
                            for chunk in iter(lambda:stream.read(1024*1024),b''):
                                self.check_stop()
                                sha.update(chunk);md5.update(chunk)
                        digest,md5_digest=sha.hexdigest(),md5.hexdigest()
                    after=path.stat()
                    if (after.st_size,after.st_mtime_ns)!=signature:
                        cacheable=False
                        self.log.emit(f'[文件变化] {path.name}；下次扫描再处理')
                        continue
                    group={t.key:t for t in self.archive_targets}
                    group.update({t.key:t for t in targets})
                    history={t.key:t for t in self.history_targets}
                    history.update(group)
                    history_errors={}
                    # Keep other platform history visible without making it a
                    # completion prerequisite for a selected-target deployment.
                    for target in history.values():
                        self.check_stop()
                        before_state=ledger.result(digest,target.key).get('status')
                        try:
                            restored=self.legacy_success(ledger,path,digest,md5_digest,target,signature)
                        except (OSError,ValueError,TypeError) as error:
                            if target.key in group:
                                raise
                            history_errors[target.key]={'status':'CONFIG_REQUIRED','message':str(error),'attempts':[]}
                            self.log.emit(f'[历史显示未完成] {target.key}：{error}；不影响本任务所选账号上传及归档')
                            continue
                        if restored.get('status')=='SUCCESS' and before_state!='SUCCESS':
                            self.log.emit(f'[历史成功迁移] {path.name} → {target.key}：同账号及文件摘要匹配，未重新上传')
                    plan=ledger.register_plan(digest,list(group.values()),fixed=True)
                    results={}
                    for key in history:
                        previous=ledger.result(digest,key)
                        if previous.get('status')=='UNCONFIRMED':
                            previous=ledger.set_result(digest,key,'FAILED',LEGACY_FAILED_MESSAGE,previous.get('info'))
                        results[key]=fresh_result(previous)
                    results.update(history_errors)
                    item={'file_name':path.name,'file_path':str(path),'size_bytes':before.st_size,
                        'mtime_ns':before.st_mtime_ns,'sha256':digest,'md5':md5_digest,'results':results,'archived_path':None,
                        'modified_at':datetime.fromtimestamp(before.st_mtime,CHINA_TIME).isoformat()}
                    if plan['required'] and all(results[key]['status'] in ('SUCCESS','IGNORED') for key in plan['required']):
                        self.check_stop()
                        try:
                            item['archived_path']=str(archive_completed_file(path,signature,self.log.emit))
                            archived+=1
                            self.log.emit(f'[清理归档] {path.name}：本次清理的{len(plan["required"])}个目标全部完成，各账号原状态保留')
                        except (OSError,ValueError) as error:
                            cacheable=False
                            item['archive_error']=str(error)
                            self.log.emit(f'[归档未完成] {path.name}：{error}；结果保留')
                    self.items.append(item)
                    self.result_changed.emit(item)
                except InterruptedError:
                    raise
                except OSError as error:
                    cacheable=False
                    self.log.emit(f'[文件暂不能处理] {path}：{error}；保留文件')
            self.check_stop()
            final_token=scan_token(self.root,self.targets,self.archive_targets,self.history_targets)
            removed={item['file_path'] for item in self.items if item['archived_path']}
            expected=tuple((key,folder,extensions,tuple(entry for entry in entries if entry[0] not in removed))
                for key,folder,extensions,entries in token[0])
            if cacheable and final_token[0]==expected and final_token[3]==token[3]:
                self.cache[cache_key]={'token':final_token,'items':self.items}
            else:
                self.cache.pop(cache_key,None)
                self.log.emit('[扫描清理] 目录仍有变化或暂不能处理的文件；启动部署时重新检查')
            self.log.emit(f'[扫描清理完成] 扫描{len(files)}个文件，归档{archived}个；历史成功不重传，历史失败及新文件待上传')
        except InterruptedError:
            self.cancelled=True
        except Exception as error:
            self.error=str(error)
            self.log.emit('[扫描清理失败] '+self.error)
