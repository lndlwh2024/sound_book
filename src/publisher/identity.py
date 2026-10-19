"""Read the signed-in creator's own identity; never use post authors or page titles."""
import re
import time
from urllib.parse import urlsplit
from .platforms import CHANNELS

IDENTITY_SELECTORS = {
    'douyin': ['#header-avatar [class*="nickname"]', '#header-avatar [class*="user-name"]',
               '#header-avatar [class*="userName"]', '#header-avatar [class*="user_name"]',
               '#header-avatar [class*="name"]', '[class*="creator-header"] [class*="name"]',
               'header [class*="user-name"]', 'header [class*="nickname"]'],
    'xiaohongshu': ['[class*="user-info"] [class*="name"]', '[class*="userInfo"] [class*="name"]',
                   'header [class*="nickname"]', 'header [class*="user-name"]'],
    'kuaishou': ['[class*="user-info"] [class*="name"]', '[class*="userInfo"] [class*="name"]',
                'header [class*="nickname"]', 'header [class*="user-name"]'],
    'youtube': ['ytcp-navigation-drawer #entity-name', 'ytcp-navigation-drawer [id="channel-name"]',
                'ytcp-channel-identity #channel-name'],
    'wechat_channels': ['#side-bar .side-bar-footer .account-info .name'],
}

# Read only the signed-in sidebar card. The current shell also uses a common
# menu account card; text can be painted by display:contents or CSS content.
WECHAT_IDENTITY_JS = r'''() => {
    const prohibited=e=>!!e.closest('article,main,[contenteditable=true],'
        +'[class*="share-video"],[class*="changeAccount"],[role=dialog]');
    const visible=e=>{
        for(let n=e;n;n=n.parentElement||n.getRootNode()?.host){
            const s=getComputedStyle(n);
            if(s.display==='none'||s.visibility==='hidden'||n.getAttribute('aria-hidden')==='true')return false;
        }
        if([...e.getClientRects()].some(r=>r.width>0&&r.height>0))return true;
        const range=document.createRange();range.selectNodeContents(e);
        return [...range.getClientRects()].some(r=>r.width>0&&r.height>0);
    };
    if([...document.querySelectorAll('input[type=password]')].some(visible))return null;
    const selectors=['#side-bar .side-bar-footer .account-info',
        '#side-bar .account-info','.side-bar-footer .account-info',
        '.common-menu-item.account-info','aside .account-info'];
    const roots=[...new Set(selectors.flatMap(s=>[...document.querySelectorAll(s)]))]
        .filter(e=>!prohibited(e)&&visible(e));
    const clean=t=>(t||'').trim();
    const valid=t=>t&&t.length<=60&&!t.includes('\n')&&/[\p{L}\p{N}]/u.test(t)
        &&!(/^(?:切换视频号|取消切换|切换|取消|登录|扫码登录|退出登录|账号设置|视频号.*助手|[>›»])$/.test(t));
    const pseudo=(e,p)=>{
        const content=getComputedStyle(e,p).content;
        if(!content||content==='none'||content==='normal')return '';
        try{return JSON.parse(content)}catch{return ''}
    };
    const names=[];
    for(const root of roots){
        const rect=root.getBoundingClientRect();
        const legacy=!!root.closest('#side-bar .side-bar-footer');
        if(!legacy&&(![...root.querySelectorAll('img')].some(visible)
            ||rect.left>Math.min(450,innerWidth*.4)||rect.width>500))continue;
        const candidates=[...root.querySelectorAll('.name,.nickname,.account-name,.user-name,'
            +'[class*="nick-name"],[class*="nickname"],[class*="account-name"],'
            +'[class*="user-name"],[data-nickname]')].filter(e=>!prohibited(e)&&visible(e));
        let found=[];
        for(const e of candidates){
            const values=[clean(e.innerText)||clean(e.textContent),pseudo(e,'::before'),pseudo(e,'::after'),
                e.getAttribute('data-nickname'),e.getAttribute('title'),e.getAttribute('aria-label')]
                .map(clean).filter(valid);
            found.push(...values);
        }
        if(!found.length){
            // Direct visible text in the avatar card, excluding account-switch
            // dialogs and nested author/editor areas, never the entire sidebar.
            const walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT);
            for(const e of root.querySelectorAll('*')){
                if(prohibited(e)||!visible(e)||e.matches('img,svg,[class*="icon"]'))continue;
                for(const text of [pseudo(e,'::before'),pseudo(e,'::after')].map(clean)){
                    if(valid(text))found.push(text);
                }
            }
            for(let node=walker.nextNode();node;node=walker.nextNode()){
                const owner=node.parentElement;
                if(owner&&!prohibited(owner)&&visible(owner)){
                    const text=clean(node.textContent);if(valid(text))found.push(text);
                }
            }
        }
        names.push(...found);
    }
    const unique=[...new Set(names)];
    return unique.length===1?{nickname:unique[0],source:'wechat-sidebar-own-account'}:
        unique.length>1?{ambiguous:true}:null;
}'''

# The current creator centre shows only #header-avatar until its account menu
# is hovered. Read only that menu (including a portal explicitly owned by it).
DOUYIN_AVATAR_IDENTITY_JS = r'''() => {
    const visible=e=>!!(e.getClientRects().length && getComputedStyle(e).visibility!=='hidden');
    if([...document.querySelectorAll('input[type=password]')].some(visible))return null;
    const triggers=[...document.querySelectorAll('#header-avatar')].filter(visible);
    if(triggers.length!==1)return null;
    const trigger=triggers[0],roots=[trigger];
    for(const attr of ['aria-controls','aria-describedby','aria-owns']){
        for(const id of (trigger.getAttribute(attr)||'').split(/\s+/)){
            const target=document.getElementById(id);
            if(target && visible(target))roots.push(target);
        }
    }
    // Some versions mount the menu next to the avatar wrapper, without ARIA.
    // Its own-account action and spatial attachment are both required.
    const avatar=trigger.getBoundingClientRect();
    for(const popup of document.querySelectorAll('[role=menu],[role=tooltip],[class*="dropdown-content"],[class*="popover-content"]')){
        if(!visible(popup)||popup.closest('article,main'))continue;
        const r=popup.getBoundingClientRect(),text=(popup.innerText||'').trim();
        if(/退出登录|退出账号|账号设置|账号管理/.test(text) && r.right>=avatar.left-80 && r.left<=avatar.right+80
            && r.top>=avatar.top-20 && r.top<=avatar.bottom+160)roots.push(popup);
    }
    const selectors=['[class*="nickname"],[class*="user-name"],[class*="userName"],[class*="user_name"]',
        '[class*="user-info"] [class*="name"],[class*="account-name"]','[class*="name"]'];
    for(const selector of selectors){
        const names=[...new Set(roots.flatMap(root=>[...root.querySelectorAll(selector)])
            .filter(e=>visible(e)&&!e.closest('article,[contenteditable=true]'))
            .map(e=>(e.innerText||'').trim()).filter(t=>t&&t.length<=60&&!t.includes('\n')))];
        if(names.length>1)return {ambiguous:true};
        if(names.length===1)return {nickname:names[0],source:'#header-avatar own-account-menu'};
    }
    return null; // Avatar alt/title labels are not reliable display names.
}'''

# The homepage can expose the current creator in a profile card rather than
# the header. Require an account-number label, avatar and a unique name in the
# same card; never read a feed author or an arbitrary page-wide "name" node.
DOUYIN_HOME_IDENTITY_JS = r'''() => {
    if(!/^\/creator-micro\/home\/?$/.test(location.pathname))return null;
    const visible=e=>!!(e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden');
    if([...document.querySelectorAll('input[type=password]')].some(visible))return null;
    const names=[];
    for(const root of document.querySelectorAll('[class*="user-info"],[class*="userInfo"],'
        +'[class*="user-profile"],[class*="userProfile"],[class*="account-info"],[class*="creator-info"]')){
        if(!visible(root)||root.closest('article,[class*="recommend"],[class*="author"]'))continue;
        const rect=root.getBoundingClientRect(),text=(root.innerText||'').trim();
        if(rect.top>400||text.length>600||!/(抖音号|账号ID|帐号ID)\s*[:：]/.test(text)
            ||![...root.querySelectorAll('img')].some(visible))continue;
        const found=[...new Set([...root.querySelectorAll('[class*="nickname"],[class*="nick-name"],'
            +'[class*="user-name"],[class*="userName"],[class*="user_name"],[class*="name"]')]
            .filter(e=>visible(e)&&!e.closest('article,[contenteditable=true]'))
            .map(e=>(e.innerText||'').trim()).filter(t=>t&&t.length<=60&&!t.includes('\n')
                &&!/(抖音号|账号ID|帐号ID)\s*[:：]/.test(t)))];
        if(found.length>1)return {ambiguous:true};
        if(found.length===1)names.push(found[0]);
    }
    const unique=[...new Set(names)];
    return unique.length===1?{nickname:unique[0],source:'creator-home own-profile-card'}:
        unique.length>1?{ambiguous:true}:null;
}'''


def _identity(result, page, platform):
    if not isinstance(result, dict) or not isinstance(result.get('nickname'), str):
        return None
    nickname = result['nickname'].strip()
    if not nickname or len(nickname) > 60 or '\n' in nickname:
        return None
    if nickname in {'登录','扫码登录','上传视频','发布','用户','创作者','你的频道','Your channel','YouTube',
                    '头像','用户头像','账号头像','创作者头像','抖音','抖音创作者中心','首页','账号设置','账号管理','退出登录'}:
        return None
    if platform=='kuaishou' and nickname=='TA':
        return None
    address = urlsplit(page.url)
    channel = re.match(r'/channel/(UC[\w-]{22})(?:/|$)', address.path) if platform == 'youtube' else None
    return {'nickname':nickname, 'platform_uid':channel.group(1) if channel else '',
            'channel_id':channel.group(1) if channel else '', 'source':result.get('source','own-profile')}


def extract_platform_identity(page, platform):
    address = urlsplit(page.url)
    if address.hostname != CHANNELS[platform].host or re.search(r'/(login|signin)(?:/|$)', address.path):
        return None
    if platform == 'wechat_channels' and not address.path.startswith('/platform/'):
        return None  # The official /login.html is on the same host.
    if platform == 'wechat_channels':
        return _identity(page.evaluate(WECHAT_IDENTITY_JS),page,platform)
    if platform == 'douyin':
        result = page.evaluate(DOUYIN_AVATAR_IDENTITY_JS)
        if isinstance(result, dict) and result.get('ambiguous'):
            return None
        if result:
            return _identity(result, page, platform)
        result = page.evaluate(DOUYIN_HOME_IDENTITY_JS)
        if isinstance(result, dict) and result.get('ambiguous'):
            return None
        if result:
            return _identity(result, page, platform)
    result = page.evaluate('''selectors => {
        const visible=e=>!!(e.getClientRects().length && getComputedStyle(e).visibility!=='hidden');
        if ([...document.querySelectorAll('input[type=password]')].some(visible)) return null;
        for (const selector of selectors) {
            const names=[...new Set([...document.querySelectorAll(selector)].filter(e=>visible(e)&&!e.closest('article,[contenteditable=true]'))
                .map(e=>(e.innerText||'').trim()).filter(t=>t && t.length<=60 && !t.includes('\\n')) )];
            if (names.length>1) return null;
            if (names.length===1) return {nickname:names[0],source:selector};
        }
        return null;
    }''', IDENTITY_SELECTORS[platform])
    return _identity(result, page, platform)


def read_homepage_identity(page, platform, stop_requested=lambda: False, seconds=5, diagnostic=None):
    """Read an isolated own-home tab, preserving the operator's current page."""
    if platform != 'douyin' or stop_requested():
        return None
    address = urlsplit(page.url)
    if address.hostname != CHANNELS[platform].host or re.search(r'/(login|signin)(?:/|$)', address.path):
        return None
    home = None
    try:
        home = page.context.new_page()
        deadline = time.monotonic()+seconds
        try:
            home.goto('https://creator.douyin.com/creator-micro/home', wait_until='domcontentloaded',
                      timeout=max(100,int(seconds*1000)))
        except Exception:
            pass  # A slow navigation can still expose a usable own-account card.
        while time.monotonic()<deadline and not stop_requested():
            identity = extract_platform_identity(home, platform)
            if identity:
                return identity
            home.wait_for_timeout(200)
        if diagnostic and not stop_requested():
            diagnostic(home)
        return None
    except Exception:
        return None  # The caller retains login state and captures its original page.
    finally:
        if home:
            try:
                home.close()
            except Exception:
                pass


def refresh_platform_identity(page, platform, stop_requested=lambda: False, seconds=5):
    """Explicit read: reveal the creator's own account menu, never click actions."""
    identity = extract_platform_identity(page, platform)
    if identity or platform != 'douyin' or stop_requested():
        return identity
    address = urlsplit(page.url)
    if address.hostname != CHANNELS[platform].host or re.search(r'/(login|signin)(?:/|$)', address.path):
        return None
    trigger = page.locator('#header-avatar:visible')
    if trigger.count() != 1:
        return None
    trigger.hover(timeout=max(100, min(1500, int(seconds*1000))))
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not stop_requested():
        identity = extract_platform_identity(page, platform)
        if identity:
            return identity
        page.wait_for_timeout(200)
    return None


def identity_read_failure(page, platform):
    """A missing display name does not mean the saved login has expired."""
    address = urlsplit(page.url)
    if (address.hostname != CHANNELS[platform].host or re.search(r'/(login|signin)(?:/|$)', address.path)
            or platform == 'wechat_channels' and not address.path.startswith('/platform/')):
        return '当前标签页不在已登录的创作者页面；请选择正确页面，确实进入登录页时再完成登录。原昵称与绑定保留。'
    state = page.evaluate(r'''() => {
        const visible=e=>!!(e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden');
        const login=[...document.querySelectorAll('button,[role=button],h1,h2,input[type=password]')].some(e=>
            visible(e)&&(e.matches('input[type=password]')||/^(扫码登录|立即登录|请先登录|登录创作者中心)$/.test((e.innerText||'').trim())));
        const creator=[...document.querySelectorAll('#header-avatar,input[type=file][accept*=video]')].some(e=>
            e.id==='header-avatar'?visible(e):!e.disabled);
        return {login,creator};
    }''')
    if state.get('login'):
        return '当前页面明确要求登录，请完成平台登录后再读取昵称；原昵称与绑定保留。'
    if state.get('creator'):
        return '已检测到账号头像或视频发布入口，但未识别到唯一的本人昵称；无需重新扫码，原昵称与绑定保留，请查看已保存的账号区域DOM。'
    return '当前页尚未取得唯一的本人昵称，可能仍在加载或账号信息区结构变化；原昵称与登录状态保留，请查看DOM诊断。'
