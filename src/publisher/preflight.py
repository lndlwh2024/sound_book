"""Account-only checks before any media job, using the existing isolated profiles.

No file selection, form filling or submission takes place here. A historical DOM
capture is structural evidence; only a fresh page can prove current access.
"""
import re
import threading
import uuid
import time
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from PySide6.QtCore import QThread, Signal
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError, Error as PlaywrightError

from .browser_workflow import BrowserPublishWorkflow
from .page_access import check_network_page, wechat_platform_path, login_page_message
from .channel_probe import CREATOR_NOTICES_JS
from .creator_studio import CreatorStudioUploader, rejection
from .dom_readiness import publishing_dom_state as account_dom_state, structure_ready
from .douyin.browser import DouyinBrowserManager
from .platforms import CHANNELS
from .status import STATUS_TEXT
from .youtube import PlatformRejected, UploadStopped, YouTubeUploader
from .youtube_browser import launch_youtube_context, google_login_rejected, GOOGLE_LOGIN_HELP, youtube_runtime_summary


LOGIN_PAGE_JS = r'''() => {
    const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);
        return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none';};
    const password=[...document.querySelectorAll('input[type=password]')].some(visible);
    const login=[...document.querySelectorAll('button,a,[role=button],div,span')].some(e=>
        visible(e)&&!e.closest('input,textarea,[contenteditable=true]')
        && /^(立即登录|扫码登录|请先登录|登录创作者中心|Sign in)$/.test((e.innerText||'').trim()));
    return {password,login};
}'''


@dataclass(frozen=True)
class PreflightResult:
    key: str
    name: str
    status: str
    message: str
    identity: dict | None = None
    nickname_missing: bool = False

    @property
    def passed(self):
        return self.status == 'SUCCESS'


def failure_message(results, *, partial=False):
    failures = [r for r in results if not r.passed]
    if not failures:
        return ''
    actions = {
        'AUTH_REQUIRED': '请点击该账号的“登录 / 获取平台昵称”完成登录，并关闭登录浏览器后重试。',
        'ACCOUNT_RESTRICTED': '请按平台提示解除账号限制后重试；重复登录不能解除封禁。',
        'IDENTITY_MISMATCH': '请核对实际账号及固定频道 ID；昵称变化不阻止发布。',
        'VERIFICATION_REQUIRED': '请由账号本人完成平台安全验证后重试。',
        'PLATFORM_BUSY': '请稍后重新启动，当前不会继续请求投稿。',
    }
    if partial:
        actions['PLATFORM_BUSY'] = '请稍后重新检查该账号，当前不会继续请求该账号投稿。'
    heading = ('以下账号暂不能部署，请分别处理；已通过检查的账号不受影响：\n\n' if partial else
               '本次部署尚未启动；请完成以下操作后重新启动：\n\n')
    return (heading
            + '\n\n'.join(f'{r.name}：{STATUS_TEXT.get(r.status, r.status)}\n{r.message}'
                + ('\n'+actions[r.status] if r.status in actions else '') for r in failures))


def prerequisite_results(targets, report_root):
    """Check every chosen account, including Douyin and YouTube, before browsers."""
    failures = []
    for target in targets:
        problems = []
        state = account_dom_state(report_root, target.platform, target.account.account_id)
        if not structure_ready(state):
            problems.append(state.message + '。缺少平台适配，请采集异常页面用于补充适配。')
        profile = Path(target.profile) if target.profile else None
        try:
            available = bool(profile and profile.is_dir() and any(profile.iterdir()))
        except OSError:
            available = False
        if not available:
            problems.append('缺少该账号的浏览器登录目录。请点击“登录 / 获取平台昵称”完成登录。')
        try:
            from .titles import resolve_title
            resolve_title(target.account,'配置校验.mp4',target.platform)
        except ValueError as error:
            problems.append(str(error))
        if target.platform == 'youtube' and not re.fullmatch(r'UC[\w-]{22}', target.account.channel_id):
            problems.append('请登录并保存正确的 YouTube 频道 ID。')
        if problems:
            failures.append(PreflightResult(target.key, target_name(target), 'CONFIG_REQUIRED', '\n'.join(problems)))
    return failures


def target_name(target):
    return f'{CHANNELS[target.platform].name} / {target.account.account_name}'


class PreflightResourceError(RuntimeError):
    """The shared browser runtime cannot safely perform further account checks."""


class AccountPreflight(BrowserPublishWorkflow):
    """Use the proven event pump and existing identity validators without upload."""
    def __init__(self, target, stop_requested=lambda: False, log=lambda message: None, seconds=30,
                 diagnostic_root=None, task_id=''):
        self.target, self.account, self.channel = target, target.account, CHANNELS[target.platform]
        self.stop_requested, self.log, self.seconds = stop_requested, log, seconds
        self.diagnostic_root,self.task_id=diagnostic_root,task_id
        from .page_access import LoginObservation
        self.login_observation=LoginObservation(min(8,seconds/2))

    def _check_stop(self):
        if self.stop_requested():
            raise UploadStopped()

    def _progress(self, message):
        self.log(message)

    def wait(self, page, label, condition, seconds=30):
        return self._wait_until(page, label, condition, min(seconds, self.seconds))

    def _check_duration_notice(self, page):
        login_message = login_page_message(page, self.target.platform)
        if login_message:
            self._observe_login(page,login_message)
        check_network_page(page.url)
        address = urlsplit(page.url)
        if self.target.platform == 'youtube' and google_login_rejected(page.url):
            raise PlatformRejected('AUTH_REQUIRED', GOOGLE_LOGIN_HELP)
        if address.hostname != self.channel.host:
            raise PlaywrightTimeoutError('平台正在跳转，尚未确认登录状态')
        if self.target.platform == 'wechat_channels' and not wechat_platform_path(address.path):
            raise PlaywrightTimeoutError('尚未进入视频号发布区域，继续等待页面稳定')
        # Same rejection evidence as the actual publisher, including a banned
        # creator landing page, verification panels and server-busy notices.
        for item in page.evaluate(CREATOR_NOTICES_JS):
            state = 'VERIFICATION_REQUIRED' if item['verification'] else rejection(item['text'])
            if state and (item['notice'] or state == 'ACCOUNT_LIMIT'
                          or item['accessPage'] and state == 'ACCOUNT_RESTRICTED'):
                raise PlatformRejected(state, item['text'])
        login = page.evaluate(LOGIN_PAGE_JS)
        if login['password'] or login['login']:
            self._observe_login(page,'当前页面持续要求登录，请在该账号的登录窗口完成登录后重试。')
        self.login_observation.observe(page,'')

    def _observe_login(self,page,message):
        if self.login_observation.observe(page,message):
            raise PlatformRejected('AUTH_REQUIRED',message)
        raise PlaywrightTimeoutError('正在等待平台自动恢复登录；尚未选择文件')

    def _confirmed_login_message(self,page):
        message=login_page_message(page,self.target.platform)
        return message if message and self.login_observation.observe(page,message) else ''

    def verify_page(self, page):
        def verify_current_document():
            self._check_stop()
            self._check_duration_notice(page)
            if self.target.platform == 'youtube':
                YouTubeUploader.verify_channel(self, page, self.seconds)
            else:
                CreatorStudioUploader.verify_identity(self,page)
            self._check_duration_notice(page)
            return True
        try:
            self.wait(page, '核对当前登录和发布入口', verify_current_document, self.seconds)
        except RuntimeError:
            # One final observation uses the current page, including a redirect
            # that settled during the last event-pump interval.
            self._check_duration_notice(page)
            raise

    def _open_platform(self,page):
        from .page_access import transient_network_error
        deadline=time.monotonic()+min(10,self.seconds)
        for attempt in range(2):
            self._check_stop()
            try:
                page.goto(self.channel.url,wait_until='domcontentloaded',
                          timeout=max(1,int((deadline-time.monotonic())*1000)))
                return
            except PlaywrightTimeoutError:
                self.log('页面加载较慢，继续检查当前页面；尚未开始上传')
                return
            except PlaywrightError as error:
                if attempt or not transient_network_error(error) or time.monotonic()>=deadline:
                    raise
                self.log('[网络重试 1/1] 平台页面暂未打开，保留同一账号浏览器后重试；未据此判定登录失效')
                remaining=min(2,max(0,(deadline-time.monotonic())/2))
                while remaining>0:
                    self._check_stop()
                    milliseconds=min(300,max(1,int(remaining*1000)))
                    page.wait_for_timeout(milliseconds)
                    remaining-=milliseconds/1000

    def check(self, playwright, headless=False):
        context, page, passed = None, None, False
        step='启动账号浏览器'
        try:
            self._check_stop()
            kwargs = dict(headless=headless, viewport={'width': 1440, 'height': 900}, timeout=30000,
                          page_timing_log=self.log)
            if self.target.platform == 'douyin':
                context = DouyinBrowserManager.create_persistent_context(playwright, Path(self.target.profile), **kwargs)
            elif self.target.platform == 'youtube':
                context = launch_youtube_context(playwright, self.target.profile, **kwargs)
                self.log(youtube_runtime_summary(context, self.target.profile))
            else:
                from .media_browser import launch_media_context
                if self.target.platform=='wechat_channels' and self.diagnostic_root is not None:
                    kwargs['wechat_auth_trace']={'root':self.diagnostic_root,'target':self.target,'log':self.log,
                        'task_id':self.task_id,'source':'部署前检查'}
                context = launch_media_context(playwright,self.target.profile, **kwargs)
            page = context.pages[0] if context.pages else context.new_page()
            step='检查浏览器视频解码'
            from .media_browser import media_capabilities
            capabilities = media_capabilities(page)
            if not capabilities or not capabilities.get('h264') or not capabilities.get('aac'):
                raise PlatformRejected('CONFIG_REQUIRED','浏览器缺少 H.264/AAC 视频解码支持，请完整更新发行包；尚未上传文件，不消耗上传重试次数')
            self.log('[浏览器媒体检查] H.264/AAC 支持通过；浏览器版本 '+str(context.browser.version))
            page.set_default_timeout(1000)
            self._check_stop()
            step='打开平台发布页'
            self._open_platform(page)
            step='检查平台登录和发布权限'
            self.verify_page(page)
            self._check_stop()
            passed = True
            self._record_auth('SUCCESS',context,page,live=True)
            return PreflightResult(self.target.key, target_name(self.target), 'SUCCESS', '当前登录与发布页访问检查通过',
                                   getattr(self,'found_identity',None))
        except PlatformRejected as error:
            return PreflightResult(self.target.key, target_name(self.target), error.status, str(error),
                                   nickname_missing=getattr(self,'nickname_missing',False))
        except UploadStopped:
            raise
        except RuntimeError as error:
            login_message = self._confirmed_login_message(page)
            if login_message:
                return PreflightResult(self.target.key, target_name(self.target), 'AUTH_REQUIRED', login_message)
            return PreflightResult(self.target.key, target_name(self.target), 'FAILED',
                str(error) + '。请检查网络和页面状态；昵称未识别时查看本人账号区域诊断，不据此判断登录失效。')
        except Exception as error:
            self.log(f'[登录检查异常] 阶段={step}；{type(error).__name__}：{error}')
            login_message = self._confirmed_login_message(page)
            if login_message:
                return PreflightResult(self.target.key, target_name(self.target), 'AUTH_REQUIRED', login_message)
            if context is None:
                detail=('浏览器启动超时（30秒）' if isinstance(error,PlaywrightTimeoutError)
                        else '浏览器启动失败（'+type(error).__name__+'）')
                return PreflightResult(self.target.key,target_name(self.target),'FAILED',
                    detail+'，尚未进入平台登录检查，未开始上传。请关闭该账号残留的浏览器窗口后重试；原登录目录已保留。')
            return PreflightResult(self.target.key, target_name(self.target), 'FAILED',
                step+'未完成，尚未开始上传（'+type(error).__name__+'）。详情已记录；'
                '请重新检查该账号，未据此认定登录失效。')
        finally:
            if not passed:
                self._capture_failure_page(page, '部署前检查未通过')
            if context:
                self._release_context(context)

    def _release_context(self,context):
        from .account_batch_session import borrowed_batch_context
        if borrowed_batch_context(context):
            self.log('[部署前会话保留] 检查使用本账号批次浏览器，后续文件继续复用')
            return
        try:
            browser=context.browser
            try:
                from .media_browser import close_media_context
                close_media_context(context)
            except Exception as error:
                self.log('[部署前会话关闭异常] '+str(error)+'；继续关闭本次检查专用浏览器')
                if browser is None:
                    raise
            if browser and browser.is_connected():
                browser.close()
            if browser and browser.is_connected():
                raise RuntimeError('部署前检查浏览器尚未关闭')
        except Exception as error:
            raise PreflightResourceError('无法确认本次检查浏览器已关闭；暂不打开后续账号的浏览器，'
                '请关闭残留检查窗口后重试：'+str(error)) from error
        self.log('[部署前会话已关闭] 检查结果随后返回；未启动其他浏览器')

    def _record_auth(self,status,context=None,page=None,live=False):
        if self.diagnostic_root is None:
            return
        try:
            from .auth_snapshots import record_auth_result
            record_auth_result(self.diagnostic_root,self.target,status,self.log,source='preflight',
                               context=context,page=page,live=live,task_id=self.task_id)
        except Exception:
            pass


class DeploymentPreflightWorker(QThread):
    log = Signal(str)
    event_logged = Signal(dict)
    status = Signal(str)
    result_ready = Signal(object)

    def __init__(self, targets, root, parent=None, task_id=None):
        super().__init__(parent)
        unique = {}
        for target in targets:
            unique.setdefault(target.key, target)
        self.targets, self.root = deepcopy(list(unique.values())), Path(root)
        from .event_log import event_log
        self.events=event_log(self.root)
        self.task_id = task_id or uuid.uuid4().hex
        from .failure_diagnostics import TaskFailureDiagnostics
        self.diagnostics = TaskFailureDiagnostics(self.root, self.task_id, self.events.task_events(self.task_id))
        self.stopping = threading.Event()
        self.results = []
        self._reported_keys = set()
        self.unsafe_resources_message = ''

    def emit_log(self,message,**extra):
        self.log.emit(message)
        target=getattr(self,'current_target',None)
        context=({'platform':target.platform,'account_id':target.account.account_id,
            'account_name':target.account.account_name} if target else {})
        event = self.events.write(message, task_id=self.task_id, **context, **extra)
        try:
            self.diagnostics.record_event(event)
        except OSError as error:
            self.log.emit('[失败诊断日志保存异常] '+str(error))
        self.event_logged.emit(event)

    def stop(self):
        self.stopping.set()

    def _record_result(self, target, result, prefix, operation):
        if target.key in self._reported_keys:
            return
        if not isinstance(result, PreflightResult) or result.key != target.key:
            result = PreflightResult(target.key, target_name(target), 'FAILED',
                '账号检查返回的身份不匹配，未将其作为本账号检查通过；请重新检查该账号。')
        self._reported_keys.add(target.key)
        self.results.append(result)
        try:
            from .auth_snapshots import record_auth_result
            record_auth_result(self.root,target,result.status,self.emit_log,source='preflight',
                               live=isinstance(getattr(self,'resource_error',None),PreflightResourceError),
                               task_id=self.task_id)
        except Exception:
            pass
        self.result_ready.emit(result)
        self.emit_log(prefix+'：'+STATUS_TEXT.get(result.status, result.status)+'；'+result.message,
                      level='INFO' if result.passed else 'ERROR')
        try:
            operation.complete(result.status, result.message)
        except OSError as error:
            self.log.emit('[失败诊断归档异常] '+str(error)+'；本账号检查结果保留，继续其他账号')

    def run(self):
        try:
            self._reported_keys.update(result.key for result in self.results if result.key)
            with ExitStack() as resources:
                playwright, resource_error = None, None
                for index, target in enumerate(self.targets, 1):
                    if self.stopping.is_set():
                        return
                    if target.key in self._reported_keys:
                        continue
                    self.current_target=target
                    prefix = f'[部署前检查 {index}/{len(self.targets)}] {target_name(target)}'
                    self.status.emit(prefix + '；尚未开始上传')
                    operation = self.diagnostics.operation(target.platform, target.account.account_id)
                    operation.auth_target, operation.auth_log = target, self.emit_log
                    try:
                        failures = prerequisite_results([target], self.root / 'dom_reports')
                    except UploadStopped:
                        raise
                    except Exception as error:
                        failures = [PreflightResult(target.key, target_name(target), 'CONFIG_REQUIRED',
                            f'本账号配置检查未完成（{type(error).__name__}）：{error}')]
                    if self.stopping.is_set():
                        return
                    if failures:
                        self._record_result(target, failures[0], prefix, operation)
                        continue
                    if playwright is None and resource_error is None:
                        try:
                            playwright = resources.enter_context(sync_playwright())
                            if playwright is None:
                                raise PreflightResourceError('浏览器运行环境未返回可用连接')
                        except UploadStopped:
                            raise
                        except Exception as error:
                            resource_error = error
                    if resource_error is not None:
                        self._record_result(target, PreflightResult(target.key,target_name(target),'FAILED',
                            '浏览器运行环境暂不可用，未执行该账号登录检查，未上传文件：'+str(resource_error)),
                            prefix, operation)
                        continue
                    self.emit_log(prefix + '：检查当前登录及发布页权限')
                    check = AccountPreflight(target, self.stopping.is_set,
                        lambda message: self.emit_log(prefix + '：' + message),
                        diagnostic_root=self.root,task_id=self.task_id)
                    check.failure_capture = operation.capture
                    try:
                        result = check.check(playwright)
                    except UploadStopped:
                        operation.complete('CANCELLED', '部署前检查被停止')
                        raise
                    except Exception as error:
                        if isinstance(error, PreflightResourceError):
                            resource_error = error
                            self.resource_error = error
                            self.unsafe_resources_message = str(error)
                        result = PreflightResult(target.key,target_name(target),'FAILED',
                            f'本账号登录检查未完成（{type(error).__name__}）：{error}；未上传文件。')
                    if self.stopping.is_set():
                        operation.complete('CANCELLED', '部署前检查被停止')
                        return
                    self._record_result(target, result, prefix, operation)
        except UploadStopped:
            self.stopping.set()
        except Exception as error:
            self.emit_log(f'[部署前检查异常] {type(error).__name__}：{error}')
            for target in self.targets:
                if target.key in self._reported_keys or self.stopping.is_set():
                    continue
                self.current_target=target
                operation=self.diagnostics.operation(target.platform,target.account.account_id)
                self._record_result(target,PreflightResult(target.key,target_name(target),'FAILED',
                    f'预检运行异常，未完成该账号检查（{type(error).__name__}）：{error}；未上传文件。'),
                    '[部署前检查未完成]',operation)
        finally:
            try:
                self.diagnostics.finish()
            except OSError as error:
                self.log.emit('[失败诊断整理异常] '+str(error))
