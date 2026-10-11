"""Account-only checks before any media job, using the existing isolated profiles.

No file selection, form filling or submission takes place here. A historical DOM
capture is structural evidence; only a fresh page can prove current access.
"""
import re
import threading
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from PySide6.QtCore import QThread, Signal
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from .browser_workflow import BrowserPublishWorkflow
from .channel_probe import CREATOR_NOTICES_JS
from .creator_studio import CreatorStudioUploader, rejection
from .dom_readiness import account_dom_state
from .douyin.browser import DouyinBrowserManager
from .identity import extract_platform_identity
from .platforms import CHANNELS
from .status import STATUS_TEXT
from .youtube import PlatformRejected, UploadStopped, YouTubeUploader


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

    @property
    def passed(self):
        return self.status == 'SUCCESS'


def failure_message(results):
    failures = [r for r in results if not r.passed]
    if not failures:
        return ''
    actions = {
        'AUTH_REQUIRED': '请点击该账号的“登录 / 获取平台昵称”完成登录，并关闭登录浏览器后重试。',
        'ACCOUNT_RESTRICTED': '请按平台提示解除账号限制后重试；重复登录不能解除封禁。',
        'IDENTITY_MISMATCH': '请登录绑定的正确账号；YouTube 还需核对频道 ID。',
        'VERIFICATION_REQUIRED': '请由账号本人完成平台安全验证后重试。',
        'PLATFORM_BUSY': '请稍后重新启动，当前不会继续请求投稿。',
    }
    return ('本次部署尚未启动；请完成以下操作后重新启动：\n\n'
            + '\n\n'.join(f'{r.name}：{STATUS_TEXT.get(r.status, r.status)}\n{r.message}'
                + ('\n'+actions[r.status] if r.status in actions else '') for r in failures))


def prerequisite_results(targets, report_root):
    """Check every chosen account, including Douyin and YouTube, before browsers."""
    failures = []
    for target in targets:
        problems = []
        state = account_dom_state(report_root, target.platform, target.account.account_id)
        if not state.publish_reports:
            problems.append(state.message + '。请点击该账号的“采集 DOM”，在视频编辑页采集。')
        profile = Path(target.profile) if target.profile else None
        try:
            available = bool(profile and profile.is_dir() and any(profile.iterdir()))
        except OSError:
            available = False
        if not available:
            problems.append('缺少该账号的浏览器登录目录。请点击“登录 / 获取平台昵称”完成登录。')
        if target.platform in ('xiaohongshu', 'kuaishou') and not (target.account.platform_nickname or '').strip():
            problems.append('尚未绑定实际昵称。请点击“登录 / 获取平台昵称”读取并保存本人账号。')
        if target.platform == 'youtube' and not re.fullmatch(r'UC[\w-]{22}', target.account.channel_id):
            problems.append('请登录并保存正确的 YouTube 频道 ID。')
        if problems:
            failures.append(PreflightResult(target.key, target_name(target), 'CONFIG_REQUIRED', '\n'.join(problems)))
    return failures


def target_name(target):
    return f'{CHANNELS[target.platform].name} / {target.account.account_name}'


class AccountPreflight(BrowserPublishWorkflow):
    """Use the proven event pump and existing identity validators without upload."""
    def __init__(self, target, stop_requested=lambda: False, log=lambda message: None, seconds=30):
        self.target, self.account, self.channel = target, target.account, CHANNELS[target.platform]
        self.stop_requested, self.log, self.seconds = stop_requested, log, seconds

    def _check_stop(self):
        if self.stop_requested():
            raise UploadStopped()

    def _progress(self, message):
        self.log(message)

    def wait(self, page, label, condition, seconds=30):
        return self._wait_until(page, label, condition, min(seconds, self.seconds))

    def _check_duration_notice(self, page):
        address = urlsplit(page.url)
        if address.hostname != self.channel.host or re.search(r'/(login|signin)(?:/|$)', address.path, re.I):
            raise PlatformRejected('AUTH_REQUIRED', '登录已失效或进入登录页，请先完成“登录 / 获取平台昵称”。')
        # Same rejection evidence as the actual publisher, including a banned
        # creator landing page, verification panels and server-busy notices.
        for item in page.evaluate(CREATOR_NOTICES_JS):
            state = 'VERIFICATION_REQUIRED' if item['verification'] else rejection(item['text'])
            if state and (item['notice'] or state == 'ACCOUNT_LIMIT'
                          or item['accessPage'] and state == 'ACCOUNT_RESTRICTED'):
                raise PlatformRejected(state, item['text'])
        login = page.evaluate(LOGIN_PAGE_JS)
        if login['password'] or login['login']:
            raise PlatformRejected('AUTH_REQUIRED', '当前页面要求登录，请点击“登录 / 获取平台昵称”完成登录，再重新启动。')

    def verify_page(self, page):
        self._check_stop()
        if self.target.platform == 'youtube':
            # Channel ID is the stable identity; remarks/nicknames may differ.
            YouTubeUploader.verify_channel(self, page, self.seconds)
        elif self.target.platform in ('xiaohongshu', 'kuaishou'):
            CreatorStudioUploader.verify_identity(self, page)
        else:
            def ready():
                identity = extract_platform_identity(page, 'douyin')
                wanted = (self.account.platform_nickname or '').strip()
                if not identity:
                    # Existing Douyin accounts may predate nickname binding.
                    # Reuse its proven video input as live protected-page proof;
                    # neither a cached status nor the URL alone is sufficient.
                    if not wanted and urlsplit(page.url).path == urlsplit(self.channel.url).path:
                        inputs = page.locator('input[type="file"][accept*="video"], input[type="file"][accept*=".mp4"]')
                        if inputs.count() == 1 and inputs.is_enabled() and inputs.evaluate(
                                "e=>!e.closest('[aria-disabled=true]')"):
                            self.log('[登录核对] 当前受保护视频上传入口可用；沿用原账号独立目录，未改写昵称绑定')
                            return True
                    return False
                if wanted and identity['nickname'] != wanted:
                    raise PlatformRejected('IDENTITY_MISMATCH',
                        f'当前昵称 {identity["nickname"]} 与绑定昵称 {wanted} 不符。请登录正确账号。')
                return identity
            self.wait(page, '核对当前抖音账号', ready, self.seconds)
        self._check_duration_notice(page)

    def check(self, playwright, headless=False):
        context = None
        try:
            self._check_stop()
            kwargs = dict(headless=headless, viewport={'width': 1440, 'height': 900}, timeout=10000)
            if self.target.platform == 'douyin':
                context = DouyinBrowserManager.create_persistent_context(playwright, Path(self.target.profile), **kwargs)
            else:
                context = playwright.chromium.launch_persistent_context(self.target.profile, **kwargs)
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(1000)
            self._check_stop()
            try:
                page.goto(self.channel.url, wait_until='domcontentloaded', timeout=10000)
            except PlaywrightTimeoutError:
                self.log('页面加载较慢，继续检查当前登录页；尚未开始上传')
            self.verify_page(page)
            self._check_stop()
            return PreflightResult(self.target.key, target_name(self.target), 'SUCCESS', '当前登录与发布页访问检查通过')
        except PlatformRejected as error:
            return PreflightResult(self.target.key, target_name(self.target), error.status, str(error))
        except UploadStopped:
            raise
        except RuntimeError as error:
            return PreflightResult(self.target.key, target_name(self.target), 'FAILED',
                str(error) + '。请检查网络并重新登录；昵称未能识别时请采集本人账号区域 DOM。')
        except Exception as error:
            return PreflightResult(self.target.key, target_name(self.target), 'FAILED',
                f'未能确认当前登录状态：{type(error).__name__}。请检查网络、关闭该账号其他浏览器，再登录或采集 DOM 后重试。')
        finally:
            if context:
                try:
                    context.close()
                except Exception:
                    pass


class DeploymentPreflightWorker(QThread):
    log = Signal(str)
    status = Signal(str)

    def __init__(self, targets, root, parent=None):
        super().__init__(parent)
        self.targets, self.root = deepcopy(targets), Path(root)
        self.stopping = threading.Event()
        self.results = []

    def stop(self):
        self.stopping.set()

    def run(self):
        try:
            failures = prerequisite_results(self.targets, self.root / 'dom_reports')
            if failures:
                self.results = failures
                return
            with sync_playwright() as playwright:
                for index, target in enumerate(self.targets, 1):
                    if self.stopping.is_set():
                        return
                    prefix = f'[部署前检查 {index}/{len(self.targets)}] {target_name(target)}'
                    self.status.emit(prefix + '；尚未开始上传')
                    self.log.emit(prefix + '：检查当前登录及发布页权限')
                    result = AccountPreflight(target, self.stopping.is_set,
                        lambda message: self.log.emit(prefix + '：' + message)).check(playwright)
                    self.results.append(result)
                    self.log.emit(prefix + '：' + STATUS_TEXT.get(result.status, result.status) + '；' + result.message)
        except UploadStopped:
            self.stopping.set()
        except Exception as error:
            self.results.append(PreflightResult('', '部署前检查', 'FAILED',
                f'检查未完成（{type(error).__name__}）。请关闭登录/采集浏览器并检查网络后重试。'))
