"""Browser error documents are network evidence, never proof of expired login."""
import re
from urllib.parse import urlsplit


def transient_navigation_error(error):
    return bool(re.search(r'execution context was destroyed|frame (?:was|has been) detached|'
        r'cannot find context with specified (?:executionContextId|id)|'
        r'navigation .* interrupted by another navigation', str(error), re.I))


def login_page_message(page, platform):
    """Only an observed login destination proves this browser needs login."""
    if page is None:
        return ''
    from .platforms import CHANNELS
    urls = [getattr(getattr(page, 'main_frame', None), 'url', ''), getattr(page, 'url', '')]
    for url in urls:
        if not isinstance(url, str):
            continue
        address = urlsplit(url)
        login = (address.hostname == CHANNELS[platform].host and bool(re.search(
            r'/(?:login|signin|sign-in)(?:\.html?)?(?:/|$)', address.path, re.I)))
        if platform == 'youtube' and address.hostname == 'accounts.google.com':
            from .youtube_browser import google_login_rejected, GOOGLE_LOGIN_HELP
            if google_login_rejected(url):
                return GOOGLE_LOGIN_HELP
            login = True
        if login:
            return (f'{CHANNELS[platform].name}已打开登录页面；当前浏览器需要登录。'
                    '请在该账号的“登录 / 获取平台昵称”窗口完成登录后重试；已成功的平台不会重新上传。')
    return ''


def check_network_page(url):
    if urlsplit(url).scheme in ('about', 'chrome-error', 'chrome', 'edge', ''):
        raise RuntimeError('浏览器尚未取得平台页面或显示网络错误；请检查VPN、PAC和代理连接后重试；未据此判定登录失效')


def wechat_platform_path(path):
    return path == '/platform' or path.startswith('/platform/')
