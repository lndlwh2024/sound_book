"""Explicit, read-only idle acceptance. Never selects a file, saves or publishes."""
import json
from pathlib import Path
import time
import uuid
from datetime import datetime

from .auth_snapshots import actual_profile,record_auth_result
from .event_log import CHINA_TIME,PublishEventLog
from .portable import atomic_write


class AccountActivityMonitor:
    def __init__(self,root,target):
        self.path=Path(root)/'logs/publisher-events.jsonl'
        self.target=target
        self.offset=self.path.stat().st_size if self.path.exists() else 0
        self.identity=self.path.stat().st_ino if self.path.exists() else None

    def changed(self):
        if not self.path.exists():return False
        stat=self.path.stat()
        if stat.st_ino!=self.identity or stat.st_size<self.offset:
            self.offset=0
            self.identity=stat.st_ino
        changed=False
        with self.path.open('r',encoding='utf-8',errors='replace') as stream:
            stream.seek(self.offset)
            for line in stream:
                try:event=json.loads(line)
                except ValueError:continue
                if (event.get('platform')==self.target.platform
                        and event.get('account_id')==self.target.account.account_id
                        and event.get('category')!='cloud'
                        and not event.get('stage','').startswith('云盘')):
                    changed=True
            self.offset=stream.tell()
        return changed


def run_idle_validation(root,target,hours,output,*,check,activity,clock=time.monotonic,
                        sleep=time.sleep,busy=lambda:False,cancelled=lambda:False):
    """Injected clock/check allows meaningful zero-wait tests without live accounts."""
    if hours not in (4,8):raise ValueError('空闲验收仅支持独立的4小时或8小时用例')
    output=Path(output)
    report={'schema':1,'task_id':uuid.uuid4().hex,'platform':target.platform,
        'account_id':target.account.account_id,'hours':hours,'mode':'read_only_upload_entry',
        'selects_media':False,'creates_draft':False,'publishes':False,
        'started_at':datetime.now(CHINA_TIME).isoformat(),'status':'STARTING'}
    def save():atomic_write(output,report)
    save()
    try:
        if busy():
            report.update(status='ACCOUNT_BUSY',message='账号浏览器正在使用，未抢占生产任务')
            return report
        baseline=check('baseline')
        report['baseline']={'status':baseline.status,'message':baseline.message,
                            'checked_at':datetime.now(CHINA_TIME).isoformat()}
        if not baseline.passed:
            report.update(status='BASELINE_FAILED',message='开始时未核验通过，未开始计时，也未上传文件')
            return report
        # Ignore this tool's initial browser check; observe subsequent production use.
        activity.changed()
        report.update(status='IDLE_WAIT',idle_started_at=datetime.now(CHINA_TIME).isoformat())
        save()
        started=clock()
        while clock()-started<hours*3600:
            if cancelled():
                report.update(status='CANCELLED',message='用户取消验收，未上传文件')
                return report
            if activity.changed():
                report.update(status='INVALIDATED_BY_ACTIVITY',message='该账号在观察期参与了任务；本次不算持续空闲，未阻塞生产任务')
                return report
            sleep(min(30,hours*3600-(clock()-started)))
        report['idle_seconds']=clock()-started
        if busy() or activity.changed():
            report.update(status='ACCOUNT_BUSY',message='到期时该账号正在使用，未抢占浏览器；本次验收未完成')
            return report
        final=check('after_idle')
        report['after_idle']={'status':final.status,'message':final.message,
                              'checked_at':datetime.now(CHINA_TIME).isoformat()}
        report.update(status='PASSED' if final.passed else 'FAILED',
            message='仅验证登录及上传入口恢复；不代表已验证真实视频上传')
        return report
    except KeyboardInterrupt:
        report.update(status='CANCELLED',message='用户终止验收，未上传文件')
        return report
    except Exception as error:
        report.update(status='CANCELLED' if cancelled() else 'CHECK_ERROR',
            message='用户取消验收，未上传文件' if cancelled() else type(error).__name__+'；详情见本次验收日志，未据此认定登录过期')
        return report
    finally:
        report['finished_at']=datetime.now(CHINA_TIME).isoformat()
        save()


def run_idle_login_check(directory,platform_and_id,hours=4,output=None):
    from .account_repository import PlatformAccountRepository
    from .douyin.config import DouyinAccountManager
    from .platforms import ChannelAccountStore
    from .multi_deploy import PublishTarget
    from .preflight import AccountPreflight,prerequisite_results
    from .windows_browser_session import profile_busy
    from .failure_diagnostics import TaskFailureDiagnostics
    from playwright.sync_api import sync_playwright
    directory=Path(directory)
    repository=PlatformAccountRepository(ChannelAccountStore(directory/'multi_platform'),
        DouyinAccountManager(directory/'douyin_accounts.json',directory/'douyin_profiles'))
    platform,separator,account_id=platform_and_id.partition(':')
    if not separator:raise ValueError('需要指定平台:稳定账号ID')
    account=next((account for account in repository.accounts(platform) if account.account_id==account_id),None)
    if account is None:raise ValueError('未找到指定账号；未新建账号或浏览器目录')
    target=PublishTarget(platform,repository.publish_account(account),repository.profile(account))
    folder=directory/'idle_login_checks'/uuid.uuid4().hex
    output=Path(output) if output else folder/'report.json'
    if output.parent.resolve().is_relative_to((directory/'idle_login_checks').resolve()):folder=output.parent
    logs=PublishEventLog(folder)
    def log(message):logs.write(message,platform=platform,account_id=account_id,category='idle_validation')
    activity=AccountActivityMonitor(repository.store.root,target)
    def check(phase):
        failures=prerequisite_results([target],repository.store.root/'dom_reports')
        if failures:return failures[0]
        # Evidence stays with this test and never replaces the live publishing task.
        evidence=TaskFailureDiagnostics(folder/'evidence',phase)
        operation=evidence.operation(platform,account_id)
        operation.auth_target=target
        operation.auth_log=log
        checker=AccountPreflight(target,lambda:(folder/'cancel.requested').exists(),log=log)
        checker.failure_capture=operation.capture
        try:
            with sync_playwright() as playwright:result=checker.check(playwright,headless=False)
            record_auth_result(repository.store.root,target,result.status,log,source='空闲验收/'+phase)
            operation.complete(result.status,result.message)
            return result
        finally:evidence.finish()
    report=run_idle_validation(repository.store.root,target,hours,output,check=check,activity=activity,
        busy=lambda:profile_busy(actual_profile(target)),cancelled=lambda:(folder/'cancel.requested').exists())
    return 0 if report['status']=='PASSED' else 1
