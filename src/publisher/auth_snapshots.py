"""Bounded login-state fingerprints. Never copy credentials or restore a session."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import threading
import uuid
from datetime import datetime

from .event_log import CHINA_TIME, redact
from .portable import atomic_write

_lock=threading.RLock()
MAX_FILE_BYTES=8*1024*1024
MAX_TOTAL_BYTES=32*1024*1024
MAX_FILES=512


def _safe(value):
    return re.sub(r'[^\w.-]','_',str(value)).strip('.')[:100] or 'unknown'


def _protect(data,decrypt=False):
    from .google_drive import _protect as protect
    return protect(data,decrypt=decrypt)


def _key(root):
    path=Path(root)/'auth_snapshots/fingerprint-key.dpapi'
    if not path.exists():
        path.parent.mkdir(parents=True,exist_ok=True)
        data=_protect(os.urandom(32))
        # Two explicitly started checks may be in different processes. A second
        # creator must never replace the key used by already written snapshots.
        try:
            descriptor=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor,'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
    return _protect(path.read_bytes(),decrypt=True)


def actual_profile(target):
    profile=Path(target.profile)
    if target.platform=='youtube':
        from .youtube_browser import selected_channel,browser_profile
        channel=selected_channel(profile)
        if channel:profile=browser_profile(profile,channel)
    return profile


def _fingerprint(key,value):
    return hmac.new(key,value if isinstance(value,bytes) else str(value).encode('utf-8'),hashlib.sha256).hexdigest()


def _inventory(profile,key):
    patterns=('Local State','Default/Preferences','Default/Network/Cookies*','Default/Cookies*',
        'Default/Local Storage/leveldb/*','Default/Session Storage/*',
        'Default/IndexedDB/*/*.ldb','Default/IndexedDB/*/*.log',
        'Default/IndexedDB/*/MANIFEST-*','Default/Sessions/*')
    files={}
    candidates={path for pattern in patterns for path in profile.glob(pattern) if path.is_file()}
    total=0
    for path in sorted(candidates)[:MAX_FILES]:
        relative=path.relative_to(profile).as_posix()
        try:
            if path.is_symlink() or not path.resolve().is_relative_to(profile.resolve()):
                files[relative]={'skipped':'linked_path'}
                continue
            before=path.stat()
            entry={'bytes':before.st_size,'modified_ns':before.st_mtime_ns}
            if before.st_size>MAX_FILE_BYTES or total+before.st_size>MAX_TOTAL_BYTES:
                entry['skipped']='size_limit'
            else:
                data=path.read_bytes()
                after=path.stat()
                total+=len(data)
                if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):
                    entry['unstable']=True
                else:
                    entry['fingerprint']=_fingerprint(key,data)
            files[relative]=entry
        except OSError as error:
            files[relative]={'unavailable':type(error).__name__}
    return files,{'files_seen':len(candidates),'files_recorded':len(files),'bytes_hashed':total,
                  'truncated':len(candidates)>MAX_FILES}


def _browser(context,key):
    result={}
    if context is None:return result
    try:
        cookies=context.cookies()
        if not isinstance(cookies,list):return result
        result['cookie_count']=len(cookies)
        result['session_cookie_count']=sum(cookie.get('expires',-1)==-1 for cookie in cookies)
        # Names, hosts, values and paths remain HMAC inputs, never log fields.
        result['cookies']={_fingerprint(key,json.dumps([cookie.get(field,'') for field in ('domain','path','name')])):
            _fingerprint(key,json.dumps([cookie.get(field) for field in ('value','expires','secure','httpOnly','sameSite')]))
            for cookie in cookies}
    except Exception as error:
        result['unavailable']=type(error).__name__
    return result


def compare_snapshots(previous,current):
    """Compare equivalent lifecycle phases; a change alone is not a root cause."""
    if not previous:
        return {'baseline_available':False,'message':'尚无同阶段的成功登录快照'}
    if previous.get('phase')!=current.get('phase') or previous.get('profile_key')!=current.get('profile_key'):
        return {'baseline_available':False,'message':'浏览器目录或采集阶段不同，未进行误导性比较'}
    old,new=previous.get('files',{}),current.get('files',{})
    changed=[name for name in old.keys()&new.keys() if old[name].get('fingerprint') and new[name].get('fingerprint')
             and old[name]['fingerprint']!=new[name]['fingerprint']]
    uncertain=[name for name,value in new.items() if value.get('unstable') or value.get('skipped') or value.get('unavailable')]
    old_browser,new_browser=previous.get('browser',{}),current.get('browser',{})
    old_cookies,new_cookies=old_browser.get('cookies',{}),new_browser.get('cookies',{})
    return {'baseline_available':True,'baseline_time':previous.get('captured_at'),
        'added_files':sorted(new.keys()-old.keys()),'missing_files':sorted(old.keys()-new.keys()),
        'changed_files':sorted(changed),'unavailable_or_unstable_files':sorted(uncertain),
        'cookie_count_before':old_browser.get('cookie_count'),'cookie_count_now':new_browser.get('cookie_count'),
        'session_cookie_count_before':old_browser.get('session_cookie_count'),
        'session_cookie_count_now':new_browser.get('session_cookie_count'),
        'cookie_fingerprints_removed':len(old_cookies.keys()-new_cookies.keys()) if old_browser and new_browser else None,
        'cookie_fingerprints_changed':sum(old_cookies[name]!=new_cookies[name] for name in old_cookies.keys()&new_cookies.keys()),
        'message':'仅记录变化；令牌正常刷新、数据库整理或文件占用也会造成变化，不能据此断言登录丢失原因'}


def record_auth_result(root,target,status,log=lambda message:None,source='',context=None,page=None,
                       live=False,task_id=''):
    """True access checks only. Errors in this diagnostic never change publishing."""
    try:
        if status in ('CANCELLED','PENDING','DAILY_LIMIT'):
            return None
        with _lock:
            root=Path(root)
            key=_key(root)
            profile=actual_profile(target)
            phase='live' if live or context is not None else 'closed'
            folder=root/'auth_snapshots'/_safe(target.platform)/_safe(target.account.account_id)
            folder.mkdir(parents=True,exist_ok=True)
            latest=folder/(phase+'-latest.json')
            previous=json.loads(latest.read_text(encoding='utf-8')) if latest.exists() else None
            files,limits=_inventory(profile,key)
            snapshot={'schema':1,'platform':target.platform,'account_id':target.account.account_id,
                'captured_at':datetime.now(CHINA_TIME).isoformat(),'phase':phase,
                'profile_key':_fingerprint(key,str(profile.resolve()).casefold()),
                'files':files,'limits':limits,'browser':_browser(context,key),
                'source':redact(source),'atomic_profile_copy':False}
            if status=='SUCCESS':
                if previous:atomic_write(folder/(phase+'-previous.json'),previous)
                atomic_write(latest,snapshot)
                summary={'status':status,'phase':phase,'captured_at':snapshot['captured_at'],
                    'files':len(files),'cookie_count':snapshot['browser'].get('cookie_count'),
                    'message':'已更新真实核验通过后的登录快照；仅保存脱敏指纹'}
                log('[登录快照更新] '+json.dumps(summary,ensure_ascii=False))
            else:
                summary={'status':status,'phase':phase,'captured_at':snapshot['captured_at'],
                         'difference':compare_snapshots(previous,snapshot)}
                # Do not overwrite a good baseline with a failed session.
                log('[登录快照对比] '+json.dumps(summary,ensure_ascii=False))
            if task_id:
                diagnostics=root/'.diagnostics'/_safe(task_id)
                diagnostics.mkdir(parents=True,exist_ok=True)
                atomic_write(diagnostics/('auth-diff-'+_safe(target.platform)+'-'+_safe(target.account.account_id)+'-'+uuid.uuid4().hex+'.json'),
                    dict(summary,platform=target.platform,account_id=target.account.account_id,source=redact(source)))
            return summary
    except Exception as error:
        try:log('[登录快照记录未完成] '+type(error).__name__+'；继续原任务，未改变登录与发布结果')
        except Exception:pass
        return None
