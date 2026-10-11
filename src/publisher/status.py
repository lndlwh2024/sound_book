"""Shared task states and readable labels, including legacy report display."""
import re


ACCOUNT_RESTRICTION = re.compile(
    r'账号(?:已|已经)?(?:被)?封禁|账号因违规操作已受到限制|账号(?:已|被)?冻结')

STATUS_TEXT = {'SUCCESS':'已成功', 'FAILED':'失败', 'FAILED1':'失败1', 'FAILED2':'失败2',
               'UNCONFIRMED':'待上传', 'CANCELLED':'待上传', 'RETRYING':'重试中',
               'IGNORED':'不再跟踪',
               'PENDING':'待上传', 'IN_PROGRESS':'上传中', 'DAILY_LIMIT':'待上传', 'ACCOUNT_LIMIT':'账号限额',
               'DURATION_LIMIT':'超长跳过', 'PERMANENT_REJECT':'永久跳过', 'CONFIG_REQUIRED':'配置未完成',
               'AUTH_REQUIRED':'需要登录', 'ACCOUNT_RESTRICTED':'账号受限',
               'VERIFICATION_REQUIRED':'需要验证', 'PLATFORM_BUSY':'平台繁忙',
               'IDENTITY_MISMATCH':'账号不符', 'FILE_CHANGED':'文件变化'}
BLOCKING = {'ACCOUNT_LIMIT', 'AUTH_REQUIRED', 'ACCOUNT_RESTRICTED', 'IDENTITY_MISMATCH',
            'CONFIG_REQUIRED', 'VERIFICATION_REQUIRED', 'PLATFORM_BUSY'}
RETRYABLE = {'FAILED', 'FAILED1'}


def result_status_text(result):
    """Display old AUTH_REQUIRED ban records accurately without rewriting them."""
    state=result['status']
    if state=='AUTH_REQUIRED' and ACCOUNT_RESTRICTION.search(result.get('message') or ''):
        state='ACCOUNT_RESTRICTED'
    if state=='FAILED' and result.get('attempt_count'):
        return '失败'+str(min(2,result['attempt_count']))
    return STATUS_TEXT.get(state,state)


def fresh_result(previous):
    """A new task keeps proven completion/skips, but gets a fresh retry budget."""
    previous = dict(previous)
    if previous.get('status') not in ('SUCCESS','IGNORED','PERMANENT_REJECT','DURATION_LIMIT'):
        previous.update(status='PENDING', attempt_count=0)
    previous['attempts'] = []
    return previous
