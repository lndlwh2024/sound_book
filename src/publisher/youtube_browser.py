"""YouTube's manual Google sign-in and automation share an app-owned profile.

Manual sign-in runs an installed browser normally, with no automation connection.
The legacy bundled profile is left intact until the user selects this login mode.
"""
import json
import os
import subprocess
from importlib.metadata import version
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from .windows_browser_session import profile_busy, close_profile_windows


GOOGLE_LOGIN_HELP = ('Google 拒绝了受自动控制的浏览器登录；请在登录窗口点击“手工登录 Google”，'
                     '使用正常 Chrome / Edge 登录，再点击“登录完成，验证频道”。'
                     '无需重新采集 DOM；普通浏览器仍被拒绝时需按 Google 提示处理。')
MARKER = 'youtube-browser.json'


@dataclass(frozen=True)
class SystemBrowser:
    channel: str
    name: str
    executable: Path


def installed_browsers():
    """Known vendor installation locations, never a user's default profile."""
    found = []
    for channel, name, relative in (
        ('chrome', 'Chrome', 'Google/Chrome/Application/chrome.exe'),
        ('msedge', 'Edge', 'Microsoft/Edge/Application/msedge.exe'),
    ):
        for variable in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA'):
            base = os.environ.get(variable)
            executable = Path(base) / relative if base else None
            if executable and executable.is_file():
                found.append(SystemBrowser(channel, name, executable))
                break
    return found


def selected_channel(profile):
    marker = Path(profile) / MARKER
    if not marker.exists():
        return None
    try:
        data = json.loads(marker.read_text(encoding='utf-8'))
        if data.get('schema') != 1 or data.get('channel') not in ('chrome', 'msedge'):
            raise ValueError('unknown browser')
        return data['channel']
    except (OSError, ValueError, AttributeError) as error:
        raise RuntimeError('YouTube 专用浏览器配置无效，请修复配置后重试；未切换至其他登录目录。') from error


def browser_profile(profile, channel):
    if channel not in ('chrome', 'msedge'):
        raise ValueError('Unsupported browser channel')
    return Path(profile) / 'youtube-system-browser' / channel


def start_manual_login(profile, start_url='https://studio.youtube.com/'):
    """Called only after the automated context closes, at the user's request.

    Caller must wait for the profile to be released before reusing it. No
    password/cookie is read or exported, and no debugging/automation flag is used.
    """
    profile = Path(profile)
    channel = selected_channel(profile)
    browsers = installed_browsers()
    browser = next((b for b in browsers if b.channel == channel), None) if channel else next(iter(browsers), None)
    if browser is None:
        raise RuntimeError('请安装或修复'+({'chrome':'Chrome', 'msedge':'Edge'}.get(channel, 'Chrome / Edge'))+
                           '，再点击“手工登录 Google”；未修改原账号的登录目录。')
    dedicated = browser_profile(profile, browser.channel)
    dedicated.mkdir(parents=True, exist_ok=True)
    if profile_busy(dedicated):
        # Resume an unfinished handoff after the login dialog/app was closed.
        return None, browser
    marker = profile / MARKER
    previous = marker.read_bytes() if marker.exists() else None
    temporary = marker.with_suffix('.tmp')
    temporary.write_text(json.dumps({'schema':1, 'channel':browser.channel}), encoding='utf-8')
    temporary.replace(marker)
    try:
        process = subprocess.Popen([
            str(browser.executable), '--user-data-dir='+str(dedicated.resolve()),
            '--no-first-run', '--no-default-browser-check', '--disable-background-mode', '--restore-last-session',
            start_url,
        ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        if previous is None:
            marker.unlink(missing_ok=True)
        else:
            marker.write_bytes(previous)
        raise
    return process, browser


def manual_browser_open(profile):
    channel = selected_channel(profile)
    return bool(channel and profile_busy(browser_profile(profile,channel)))


class ManualBrowserHandoff:
    def __init__(self, profile, browser, process):
        self.profile = browser_profile(profile,browser.channel)
        self.browser, self.process = browser, process
        self.observed = process is None
        self.free_samples = 0

    def released(self):
        busy = profile_busy(self.profile)
        self.observed = self.observed or busy
        exited = self.process is not None and self.process.poll() is not None
        ready = not busy and (self.observed or exited)
        self.free_samples = self.free_samples + 1 if ready else 0
        return self.free_samples >= 3

    def close_windows(self):
        return close_profile_windows(self.profile,self.browser.executable)


def launch_youtube_context(playwright, profile, **kwargs):
    channel = selected_channel(profile)
    if channel is None:
        from .media_browser import launch_media_context
        return launch_media_context(playwright,str(profile), **kwargs)
    from .account_batch_session import current_account_session
    batch=current_account_session()
    if batch and not batch.closing:
        existing=batch.existing(browser_profile(profile,channel))
        if existing is not None:
            return existing
    browser = next((b for b in installed_browsers() if b.channel == channel), None)
    if browser is None:
        raise RuntimeError('YouTube 专用'+({'chrome':'Chrome', 'msedge':'Edge'}[channel])+
                           '不可用，请恢复安装；为保留登录状态，未回退至内置浏览器。')
    if profile_busy(browser_profile(profile,channel)):
        raise RuntimeError('YouTube 手工登录浏览器尚未释放账号目录，请点击“登录完成，验证频道”或关闭该专用浏览器窗口后重试。')
    from .media_browser import persistent_context_options, open_persistent_context
    return open_persistent_context(playwright.chromium,
        str(browser_profile(profile, channel)), channel=channel,
        executable_path=str(browser.executable), **persistent_context_options(kwargs,browser_profile(profile,channel)))


def google_login_rejected(url):
    address = urlsplit(url)
    return address.hostname == 'accounts.google.com' and address.path.rstrip('/') in (
        '/signin/rejected', '/v3/signin/rejected', '/v2/signin/rejected')


def youtube_runtime_summary(context, profile):
    channel = selected_channel(profile)
    actual = getattr(getattr(context, 'browser', None), 'version', '未知')
    return (f'[YouTube浏览器] Playwright {version("playwright")}；'
            f'{dict(chrome="Chrome", msedge="Edge").get(channel, "内置Chromium")} {actual}；'
            '账号专用目录；当前登录和频道仍需现场验证')
