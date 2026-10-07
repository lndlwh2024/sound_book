"""Read the signed-in creator's own identity; never use post authors or page titles."""
import re
from urllib.parse import urlsplit
from .platforms import CHANNELS

IDENTITY_SELECTORS = {
    'douyin': ['[class*="creator-header"] [class*="name"]',
               '[class*="user-info"] [class*="nickname"]', 'header [class*="user-name"]',
               '[class*="user-info"] [class*="user-name"]'],
    'xiaohongshu': ['[class*="user-info"] [class*="name"]', '[class*="userInfo"] [class*="name"]',
                   'header [class*="nickname"]', 'header [class*="user-name"]'],
    'kuaishou': ['[class*="user-info"] [class*="name"]', '[class*="userInfo"] [class*="name"]',
                'header [class*="nickname"]', 'header [class*="user-name"]'],
    'youtube': ['ytcp-navigation-drawer #entity-name', 'ytcp-navigation-drawer [id="channel-name"]',
                'ytcp-channel-identity #channel-name'],
}


def extract_platform_identity(page, platform):
    address = urlsplit(page.url)
    if address.hostname != CHANNELS[platform].host or re.search(r'/(login|signin)(?:/|$)', address.path):
        return None
    result = page.evaluate('''selectors => {
        const visible=e=>!!(e.getClientRects().length && getComputedStyle(e).visibility!=='hidden');
        if ([...document.querySelectorAll('input[type=password]')].some(visible)) return null;
        for (const selector of selectors) {
            const names=[...new Set([...document.querySelectorAll(selector)].filter(visible)
                .map(e=>(e.innerText||'').trim()).filter(t=>t && t.length<=60 && !t.includes('\\n')) )];
            if (names.length>1) return null;
            if (names.length===1) return {nickname:names[0],source:selector};
        }
        return null;
    }''', IDENTITY_SELECTORS[platform])
    if not isinstance(result, dict) or not isinstance(result.get('nickname'), str):
        return None
    nickname = result['nickname'].strip()
    if nickname in {'登录','扫码登录','上传视频','发布','用户','创作者','你的频道','Your channel','YouTube'}:
        return None
    channel = re.match(r'/channel/(UC[\w-]{22})(?:/|$)', address.path) if platform == 'youtube' else None
    return {'nickname':nickname, 'platform_uid':channel.group(1) if channel else '',
            'channel_id':channel.group(1) if channel else '', 'source':result.get('source','own-profile')}
