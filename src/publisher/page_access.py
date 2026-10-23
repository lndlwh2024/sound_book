"""Browser error documents are network evidence, never proof of expired login."""
from urllib.parse import urlsplit


def check_network_page(url):
    if urlsplit(url).scheme in ('about', 'chrome-error', 'chrome', 'edge', ''):
        raise RuntimeError('浏览器尚未取得平台页面或显示网络错误；请检查VPN、PAC和代理连接后重试；未据此判定登录失效')


def wechat_platform_path(path):
    return path == '/platform' or path.startswith('/platform/')
