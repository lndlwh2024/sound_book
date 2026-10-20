"""Read-only DOM sampling across frames and open shadow roots, grouped by session."""
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .platforms import CHANNELS

STAGES = ('自动识别', '上传前', '上传中', '上传完成', '标题与正文', '封面', '合集或播放列表', '视频元素', '检查', '发布前', '发布结果', '异常提示')

# Used by both the uploader and diagnostics. Some security panels have no
# dialog/alert role, and their headings are ordinary non-semantic elements.
CREATOR_NOTICES_JS = r'''() => {
    const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);
        return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none';};
    const editable=e=>!!e.closest('input,textarea,[contenteditable=true]');
    const noticeSelector='[role=alert],[role=dialog],[aria-live=assertive],'
        +'[class*="message-error"],[class*="message-warning"],[class*="notification"],'
        +'[class*="toast"],[class*="error"],[class*="warning"]';
    const accessPage=![...document.querySelectorAll(
        'input[type=file],input[placeholder="填写标题会有更多赞哦"],#work-description-edit[contenteditable=true]')].some(visible);
    const elements=[...document.querySelectorAll('*')];
    const panels=elements.filter(e=>{
        if(!visible(e)||editable(e)||e.matches('body,html'))return false;
        const text=(e.innerText||'').trim();
        if(text.length>600||!/扫码验证|安全验证|人机验证|滑块验证/.test(text))return false;
        const modal=e.closest('[role=dialog],[aria-modal=true],[class*="modal" i],'
            +'[class*="captcha" i],[class*="verify" i],[class*="verification" i]');
        const challenge=/二维码|请.{0,30}扫码|拖动.{0,30}滑块|请求.{0,20}频繁/.test(text);
        // The current XHS panel is plain DOM with a heading, refresh and help.
        return !!modal&&challenge || /问题反馈/.test(text)&&
            (/刷新/.test(text)||/二维码.{0,15}(失效|过期)|扫码验证身份/.test(text));
    });
    const messages=elements.filter(e=>{
        const text=(e.innerText||'').trim();
        const access=/账号(?:已|已经)?(?:被)?封禁|无法访问创作者中心|账号因违规操作已受到限制/.test(text);
        return (!e.children.length||access&&text.length<350)&&visible(e)&&!editable(e);
    }).map(e=>({text:(e.innerText||'').trim(),accessPage,
        notice:!!e.closest(noticeSelector),verification:panels.some(p=>p.contains(e))}))
        .filter(m=>m.text.length>0&&m.text.length<350);
    for(const panel of panels){
        if(panels.some(p=>p!==panel&&panel.contains(p)))continue;
        messages.unshift({text:(panel.innerText||'').trim().slice(0,350),
            accessPage,notice:true,verification:true});
    }
    return messages;
}'''

EXTRACT_JS = r'''() => {
    const nodes = [], roots = [], notices = [];
    const walk = (root, path) => {
        roots.push(path);
        for (const el of root.querySelectorAll('*')) {
            if (el.shadowRoot) walk(el.shadowRoot, path + ' > ' + el.tagName.toLowerCase() + '#' + el.id);
            // The current Xiaohongshu footer exposes this closed root on its host.
            // Inspect the actual controls; never change its mode or trigger events.
            const publishRoot=el.localName==='xhs-publish-btn' && el._sr instanceof ShadowRoot
                && el._sr.host===el ? el._sr : null;
            if (publishRoot && publishRoot!==el.shadowRoot) walk(publishRoot,path+' > xhs-publish-btn#'+el.id+' (closed)');
            const noticeText = (el.innerText||el.textContent||'').trim();
            if (!el.children.length && /上传|转码|处理中|发布成功|发布失败|上限|限额|分钟|封禁|无法访问创作者中心/.test(noticeText)) {
                const nr=el.getBoundingClientRect();
                if(nr.width>0 && nr.height>0) notices.push({text:noticeText.slice(0,300), root:path});
            }
            const r = el.getBoundingClientRect(), s = getComputedStyle(el);
            const ownHome=/^\/creator-micro\/home\/?$/.test(location.pathname) &&
                !!el.closest('[class*="user-info"],[class*="userInfo"],[class*="user-profile"],[class*="userProfile"],[class*="account-info"],[class*="creator-info"]');
            const ownWechat=!!el.closest('#side-bar .account-info,.side-bar-footer .account-info,.common-menu-item.account-info,aside .account-info');
            const profileNode=ownHome || ownWechat || !!el.closest('#header-avatar,#side-bar .side-bar-footer .account-info') ||
                el.matches('[class*="user-info"],[class*="nickname"],[class*="user-name"],[class*="userName"],[class*="user_name"]')
                && !!el.closest('[role=menu],[role=tooltip],[class*="dropdown-content"],[class*="popover-content"]');
            const semantic=profileNode || el.isContentEditable || el.matches('input,textarea,button,select,option,a[href],label,[role],[contenteditable],video,progress,canvas,xhs-publish-btn,[class*="progress"],[onclick],[tabindex],[class*="button" i],[class*="btn" i]');
            if (!semantic && !(s.cursor==='pointer' && noticeText.length>0 && noticeText.length<=80)) continue;
            nodes.push({tag:el.tagName.toLowerCase(), id:el.id, name:el.getAttribute('name'),
                role:el.getAttribute('role'), type:el.getAttribute('type'), accept:el.getAttribute('accept'),
                placeholder:el.getAttribute('placeholder') || el.getAttribute('data-placeholder'),
                ariaLabel:el.getAttribute('aria-label'), ariaValueNow:el.getAttribute('aria-valuenow'),
                ariaChecked:el.getAttribute('aria-checked'), checked:el.checked===true,
                ariaSelected:el.getAttribute('aria-selected'), href:el.getAttribute('href'),
                ariaControls:el.getAttribute('aria-controls'), ariaDescribedby:el.getAttribute('aria-describedby'),
                ariaOwns:el.getAttribute('aria-owns'), title:el.getAttribute('title'), alt:el.getAttribute('alt'),
                ownAccountLabel:ownWechat ? {text:el.textContent?.trim().slice(0,200),
                    nickname:el.getAttribute('data-nickname'),before:getComputedStyle(el,'::before').content,
                    after:getComputedStyle(el,'::after').content} : null,
                ariaValueMax:el.getAttribute('aria-valuemax'), className:el.getAttribute('class'),
                disabled:el.matches(':disabled') || el.getAttribute('aria-disabled')==='true',
                disabledAttribute:el.hasAttribute('disabled'), ariaDisabled:el.getAttribute('aria-disabled'),
                pointerEvents:s.pointerEvents,
                publishComponent:el.localName==='xhs-publish-btn' ? {
                    rootMode:publishRoot?.mode||null,isPublish:el.getAttribute('is-publish'),
                    submitText:el.getAttribute('submit-text'),disabled:el.getAttribute('submit-disabled'),
                    loading:el.getAttribute('submit-loading')} : null,
                actionOwner:(()=>{const owner=el.closest('button,[role=button],[role=combobox],[role=option]');
                    return owner ? {tag:owner.tagName.toLowerCase(),className:owner.getAttribute('class'),
                        disabled:owner.matches(':disabled')||owner.getAttribute('aria-disabled')==='true',
                        pointerEvents:getComputedStyle(owner).pointerEvents} : null;})(),
                readOnly:el.readOnly===true, contentEditable:el.isContentEditable,
                editorContent:el.isContentEditable ? {attribute:el.getAttribute('contenteditable'),
                    textContent:(el.textContent||'').slice(0,1000),innerText:(el.innerText||'').slice(0,1000),
                    placeholder:el.getAttribute('data-placeholder')||el.getAttribute('placeholder'),
                    nonText:!!el.querySelector('img,video,audio,iframe,a[href],[data-mention]')} : null,
                visible:r.width>0 && r.height>0 && s.visibility!=='hidden' && s.display!=='none',
                text:el.matches('input,textarea') ? '' : (el.innerText||el.textContent||'').trim().slice(0,200),
                valueNow:el.matches('progress') ? el.value : null, valueMax:el.matches('progress') ? el.max : null,
                videoReadyState:el.matches('video') ? el.readyState : null,
                root:path});
        }
    };
    walk(document, 'document');
    return {title:document.title, nodes, roots, notices, blockingNotices:('''+CREATOR_NOTICES_JS+r''')(),
        limitation:'Closed roots are inspected only for exposed xhs-publish-btn roots; other closed roots and native browser prompts are unavailable'};
}'''


def safe_url(url):
    parsed = urlsplit(url)
    if parsed.scheme in ('http', 'https'):
        return f'{parsed.scheme}://{parsed.netloc}{parsed.path}'
    return parsed.scheme + ':'


class ChannelDOMProbe:
    def __init__(self, account, output_root):
        self.account = account
        self.channel = CHANNELS[account.platform]
        self.directory = Path(output_root) / account.platform / account.account_id / uuid.uuid4().hex
        self.index = {'platform': account.platform, 'account_id': account.account_id,
                      'account_name': account.account_name, 'captures': []}

    def capture(self, page, stage):
        if stage not in STAGES:
            raise ValueError('未知采集阶段')
        if urlsplit(page.url).hostname != self.channel.host:
            raise ValueError('当前页面不是该平台创作者页面，请先完成登录并进入发布页再采集')
        if stage == '自动识别':
            stage = self.infer_stage(page)
        self.directory.mkdir(parents=True, exist_ok=True)
        stem = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '_' + uuid.uuid4().hex[:8]
        report = self.directory / (stem + '.json')
        snapshot = self.directory / (stem + '.png')
        data = {'schema_version': 1, 'platform': self.account.platform,
                'account_id': self.account.account_id, 'stage': stage,
                'timestamp': datetime.now(timezone.utc).isoformat(), 'url': safe_url(page.url), 'frames': []}
        for frame in page.frames:
            item = {'url': safe_url(frame.url), 'name': frame.name}
            # Do not sample external authentication/third-party frame contents.
            if frame != page.main_frame and urlsplit(frame.url).hostname not in (None, self.channel.host):
                item['skipped'] = '外部来源iframe仅记录地址'
            else:
                try:
                    item.update(frame.evaluate(EXTRACT_JS))
                except Exception as error:
                    item['error'] = str(error)[:300]
            data['frames'].append(item)
        report.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        image_path = ''
        try:
            page.screenshot(path=str(snapshot), full_page=True, timeout=8000)
            image_path = str(snapshot)
        except Exception:
            try:
                page.screenshot(path=str(snapshot), full_page=False, timeout=5000)
                image_path = str(snapshot)
            except Exception as error:
                data['screenshot_error'] = str(error)[:300]
                report.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        self.index['captures'].append({'stage': stage, 'report': report.name,
                                       'snapshot': Path(image_path).name if image_path else None})
        temporary = self.directory / 'session.tmp'
        temporary.write_text(json.dumps(self.index, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(self.directory / 'session.json')
        return str(report), image_path

    @staticmethod
    def infer_stage(page):
        active = page.locator('[role=tab][aria-selected=true]:visible')
        if active.count() == 1:
            text = active.inner_text().strip()
            return {'详细信息':'标题与正文','视频元素':'视频元素','检查':'检查','公开范围':'发布前'}.get(text,'上传前')
        published = page.get_by_text(re.compile(r'^(?:视频已发布|(?:笔记|作品|视频)?发布成功[！!。\s]*|发表成功[！!。\s]*)$'))
        if any(published.nth(i).is_visible() for i in range(published.count())):
            return '发布结果'
        if page.locator('[contenteditable=true][aria-label*="标题"]:visible').count():
            return '标题与正文'
        if (urlsplit(page.url).hostname == 'channels.weixin.qq.com'
                and page.locator('.post-desc-box .input-editor[contenteditable]:visible').count()):
            return '标题与正文'
        if page.locator('input[placeholder="填写标题会有更多赞哦"]:visible, #work-description-edit[contenteditable=true]:visible').count():
            busy=page.get_by_text(re.compile(r'^(?:正在上传|上传中|正在转码|转码中)'))
            if any(busy.nth(i).is_visible() for i in range(busy.count())):
                return '上传中'
            return '标题与正文'
        return '上传前'
