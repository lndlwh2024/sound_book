"""Read the signed-in creator's own identity; never use post authors or page titles."""
import re
import time
from urllib.parse import urlsplit
from playwright.sync_api import Error as PlaywrightError
from .platforms import CHANNELS


def validated_custom_nickname(value):
    value = str(value).strip()
    if not value or len(value) > 60 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('请输入 1–60 个字符的昵称，不包含换行或控制字符')
    return value


def sync_current_nickname(page, account, platform, log=lambda message: None, changed=None, *, passive=True):
    """Nickname is optional display metadata, never an upload credential."""
    def note(message):
        try:
            log(message)
        except Exception:
            pass
    try:
        identity = extract_platform_identity(page, platform)
        if not identity and platform == 'douyin' and not passive:
            identity = refresh_platform_identity(page, platform, seconds=.4)
        if not identity:
            return None
        nickname = validated_custom_nickname(identity['nickname'])
        old = account.platform_nickname
        account.platform_nickname = nickname
        account.account_name = getattr(account, 'custom_nickname', '') or nickname
        if callable(changed):
            changed(identity)
        if old != nickname:
            note(f'[昵称同步] {old or "未记录"} → {nickname}；账号ID和发布记录保持不变')
        return identity
    except Exception as error:
        # Reading/saving a display name must never reject an otherwise valid
        # upload. Login and stable-channel checks are performed separately.
        note(f'[昵称同步未完成] {type(error).__name__}：{error}；继续发布')
        return None


def protected_upload_access(page, platform):
    """Retry a read-only access probe if navigation invalidates its DOM snapshot."""
    initial_url = page.url
    try:
        accessible = _read_protected_upload_access(page, platform)
    except PlaywrightError as error:
        # Wujie can remove a child frame while redirecting to login. The next
        # bounded wait iteration checks the current URL for AUTH_REQUIRED.
        # Do not turn a detached snapshot into an upload failure or accept it
        # as authentication; unrelated browser errors must remain visible.
        if re.search(r'frame (?:was|has been) detached|execution context was destroyed|'
                     r'cannot find context with specified (?:executionContextId|id)',
                     str(error), re.I):
            return False
        raise
    return bool(accessible and page.url == initial_url)


def _read_protected_upload_access(page, platform):
    """Read-only proof of a live upload form in this account's isolated profile.

    A user label is not an authentication credential. Hidden file inputs are
    normal; the enabled video control plus the visible platform form must exist.
    Prefer the main page, which already pierces open shadow roots in Wujie.
    """
    address = urlsplit(page.url)
    channel = CHANNELS[platform]
    if (address.scheme != 'https' or address.hostname != channel.host
            or re.search(r'/(?:login|signin)(?:/|$)', address.path, re.I)):
        return False
    if platform == 'wechat_channels' and address.path.rstrip('/') != '/platform/post/create':
        return False
    selector = 'input[type=file][accept*="video"], input[type=file][accept*=".mp4"]'
    scopes = [page]
    if not page.locator(selector).count():
        scopes += [frame for frame in page.frames if frame != page.main_frame
                   and urlsplit(frame.url).hostname == channel.host]
    for scope in scopes:
        if scope.locator('input[type=password]:visible').count():
            return False
        prompts=scope.get_by_text(re.compile(r'^(?:立即登录|扫码登录|请先登录|登录创作者中心|Sign in)$'))
        if any(prompts.nth(i).is_visible() for i in range(prompts.count())):
            return False
        inputs = scope.locator(selector)
        if inputs.count() != 1 or not inputs.is_enabled() or not inputs.evaluate(
                "e=>e.isConnected&&!e.closest('[aria-disabled=true]')"):
            continue
        if platform == 'wechat_channels':
            editor = scope.locator('input[placeholder*="短标题"]:visible')
            avatar = page.locator('.common-menu-item.account-info img.avatar:visible, #side-bar .side-bar-footer .account-info img:visible')
            own_card=page.locator('#side-bar .side-bar-footer .account-info:visible, '
                                  '.common-menu-item.account-info:visible')
            if not avatar.count() and not own_card.count():
                continue
        elif platform in ('kuaishou','xiaohongshu'):
            # Their title editors appear only after choosing a video. Login
            # checks must stay read-only and must not wait for that later form.
            own_avatar=page.locator('header [class*="avatar"]:visible, '
                '[class*="user-info"] img:visible, [class*="userInfo"] img:visible')
            own_card=page.locator('header [class*="user-info"]:visible, header [class*="userInfo"]:visible')
            return bool(address.path.rstrip('/') == urlsplit(channel.url).path.rstrip('/')
                        and (own_card.count() or own_avatar.count()))
        else:
            return address.path.rstrip('/') == urlsplit(channel.url).path.rstrip('/')
        if editor.count() == 1 and editor.is_enabled():
            return True
    return False

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
    const scopes=[document];
    for(const scope of scopes)for(const e of scope.querySelectorAll('*')){
        if(e.shadowRoot)scopes.push(e.shadowRoot);
    }
    const query=selector=>scopes.flatMap(scope=>[...scope.querySelectorAll(selector)]);
    const prohibited=e=>{
        for(let n=e;n;n=n.getRootNode()?.host){
            if(n.closest('article,main,[contenteditable=true],'
                +'[class*="share-video"],[class*="changeAccount"],[role=dialog]'))return true;
        }
        return false;
    };
    const visible=e=>{
        for(let n=e;n;n=n.parentElement||n.getRootNode()?.host){
            const s=getComputedStyle(n);
            if(s.display==='none'||s.visibility==='hidden'||n.getAttribute('aria-hidden')==='true')return false;
        }
        if([...e.getClientRects()].some(r=>r.width>0&&r.height>0))return true;
        const range=document.createRange();range.selectNodeContents(e);
        return [...range.getClientRects()].some(r=>r.width>0&&r.height>0);
    };
    if(query('input[type=password]').some(visible))return null;
    const selectors=['#side-bar .side-bar-footer .account-info',
        '#side-bar .account-info','.side-bar-footer .account-info',
        '.common-menu-item.account-info','aside .account-info'];
    const roots=[...new Set(selectors.flatMap(query))]
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
    from .page_access import wechat_platform_path
    address = urlsplit(page.url)
    if address.hostname != CHANNELS[platform].host or re.search(r'/(login|signin)(?:/|$)', address.path):
        return None
    if platform == 'wechat_channels' and not wechat_platform_path(address.path):
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
    from .page_access import wechat_platform_path
    address = urlsplit(page.url)
    if (address.hostname != CHANNELS[platform].host or re.search(r'/(login|signin)(?:/|$)', address.path)
            or platform == 'wechat_channels' and not wechat_platform_path(address.path)):
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
