"""Latest live account checks, separate from historical account configuration.

Only fresh browser checks may call apply_account_health. Nickname reads, manual
confirmations and restored file reports are deliberately not authentication.
"""
import json
import threading
from datetime import datetime
from pathlib import Path

from .event_log import CHINA_TIME, redact
from .platforms import CHANNELS
from .portable import atomic_write
from .status import STATUS_TEXT


_lock=threading.RLock()
_FAILURES={'FAILED','FAILED1','FAILED2','CHECK_FAILED','PREFLIGHT_FAILED'}
_OTHER={'ACCOUNT_LIMIT','DAILY_LIMIT','ACCOUNT_RESTRICTED','VERIFICATION_REQUIRED',
        'IDENTITY_MISMATCH','CONFIG_REQUIRED','PLATFORM_BUSY','CANCELLED','FILE_CHANGED'}


def _moment(value=None):
    result=datetime.fromisoformat(value) if isinstance(value,str) else value
    result=result or datetime.now(CHINA_TIME)
    if not isinstance(result,datetime):
        raise ValueError('账号检查时间无效')
    if result.tzinfo is None:
        result=result.replace(tzinfo=CHINA_TIME)
    return result.astimezone(CHINA_TIME)


def _key(platform,account_id):
    if platform not in CHANNELS or not isinstance(account_id,str) or not account_id:
        raise ValueError('账号检查缺少有效平台或稳定账号ID')
    return platform+':'+account_id


class AccountHealthStore:
    """Small runtime sidecar; no new fields are added to legacy account JSON."""
    def __init__(self,root):
        self.path=Path(root)/'account_health.json'

    def _read(self):
        try:
            data=json.loads(self.path.read_text(encoding='utf-8-sig'))
        except FileNotFoundError:
            return {}
        if (not isinstance(data,dict) or data.get('schema')!=1
                or not isinstance(data.get('accounts'),dict)):
            raise ValueError('账号检查记录格式无效，原文件保留')
        for value in data['accounts'].values():
            if (not isinstance(value,dict) or not isinstance(value.get('state'),str)
                    or any(not isinstance(value.get(k,''),str) for k in ('message','source','checked_at'))):
                raise ValueError('账号检查记录格式无效，原文件保留')
        return data['accounts']

    def get(self,platform,account_id):
        with _lock:
            return dict(self._read().get(_key(platform,account_id),{}))

    def put(self,platform,account_id,record):
        with _lock:
            accounts=self._read()
            accounts[_key(platform,account_id)]=dict(record)
            atomic_write(self.path,{'schema':1,'accounts':accounts})


def apply_account_health(repository,platform,account_id,status,message='',*,source='',checked_at=None):
    """Persist one live result for the stable ID; never recreate removed accounts.

    AUTH_REQUIRED changes the actual authorization marker immediately. Ordinary
    failed checks retain the authorization history and record a separate failure.
    Older timestamped callbacks cannot overwrite a newer completed live check.
    """
    status='FAILED' if status in _FAILURES else status
    if status not in {'SUCCESS','AUTH_REQUIRED'}|_OTHER|{'FAILED'}:
        raise ValueError('不支持的账号检查结果：'+str(status))
    moment=_moment(checked_at)
    with _lock:
        account=next((a for a in repository.accounts(platform) if a.account_id==account_id),None)
        if account is None:
            return None
        store=AccountHealthStore(repository.store.root)
        previous=store.get(platform,account_id)
        if previous.get('checked_at') and _moment(previous['checked_at'])>moment:
            return account
        if status in ('SUCCESS','AUTH_REQUIRED'):
            account=repository.set_login_status(platform,account_id,
                'AUTHORIZED' if status=='SUCCESS' else 'AUTH_REQUIRED',
                checked_at=moment.isoformat() if status=='SUCCESS' else None)
            if account is None:
                return None
        store.put(platform,account_id,{'state':status,'message':redact(message)[:1600],
            'source':redact(source)[:120],'checked_at':moment.isoformat()})
        return account


def account_health_view(repository,account):
    """One display contract for the selector, login label and matrix runtime row."""
    if account is None:
        return {'state':'UNVERIFIED','label':'尚未登录','message':'请先添加账号',
                'color':'#D69E2E','tooltip':'请先添加账号'}
    try:
        health=AccountHealthStore(repository.store.root).get(account.platform,account.account_id)
    except (OSError,ValueError,TypeError):
        health={'state':'FAILED','message':'账号检查记录无法读取；请重新检查，原文件保留'}
    state=health.get('state','UNVERIFIED')
    historical_expired=account.login_status in ('AUTH_REQUIRED','EXPIRED','UNAUTHORIZED')
    if state=='SUCCESS' and historical_expired:
        # A partial persistence failure must not resurrect an expired marker.
        state='AUTH_REQUIRED'
    if state=='AUTH_REQUIRED' or state=='UNVERIFIED' and account.login_status in ('AUTH_REQUIRED','EXPIRED'):
        state,label,color='AUTH_REQUIRED','需要登录','#E53E3E'
        message=health.get('message') or '最近一次实际检查要求重新登录，请完成登录后再次核验'
    elif state=='SUCCESS':
        label,color='核验通过','#38A169'
        message=health.get('message') or '最近一次实际登录及投稿入口检查通过'
    elif state=='FAILED' or state in _FAILURES:
        label,color='检查失败','#D69E2E'
        message=health.get('message') or '本次检查未完成；不能据此判断登录已失效'
    elif state in _OTHER:
        label,color=('检查已取消' if state=='CANCELLED' else STATUS_TEXT.get(state,'检查未通过')),'#D69E2E'
        message=health.get('message') or label
    else:
        label,color='待检查','#D69E2E'
        message=('保留历史授权记录，当前登录状态尚待实际检查'
                 if account.login_status in ('AUTHORIZED','USER_CONFIRMED') else '尚无实际登录及投稿入口检查结果')
    details=[message]
    if health.get('checked_at'):details.append('检查时间：'+health['checked_at'])
    if health.get('source'):details.append('检查来源：'+health['source'])
    if account.last_auth_time:details.append('最近通过验证：'+str(account.last_auth_time))
    return {'state':state,'label':label,'message':message,'color':color,'tooltip':'\n'.join(details)}
