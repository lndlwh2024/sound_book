"""Shared upload / confirmed-publication pipeline for Xiaohongshu and Kuaishou."""
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from .browser_workflow import BrowserPublishWorkflow
from .channel_probe import ChannelDOMProbe, safe_url, CREATOR_NOTICES_JS
from .dom_readiness import publishing_dom_state as account_dom_state, structure_ready
from .page_structure import file_input_selector
from .douyin.uploader import resolve_publish_series
from .identity import extract_platform_identity
from .media import media_type
from .platforms import CHANNELS
from .status import ACCOUNT_RESTRICTION
from .youtube import UploadStopped, PlatformRejected


SUCCESS_NOTICE = re.compile(r'^(?:笔记|作品|视频)?发布成功[！!。\s]*(?:.*审核.*)?$')


def rejection(message):
    if re.search(r'(今日|今天|每日|当天|当日).*(达到|已达|超过|超出|用尽|用完|已满).*(上限|限额|最大数量)|'
                 r'(达到|已达|超过|超出).*(今日|今天|每日|当天|当日).*(上限|限额)|'
                 r'(投稿|发布|上传).*(已达上限|已达到上限|数量已超限|次数已用完|次数已用尽)',message):
        return 'ACCOUNT_LIMIT'
    if re.search(r'(视频|时长).*(超过|超出|不得|不能|过长|限制|应为|需在).*(分钟|小时|秒)|视频过长',message):
        return 'DURATION_LIMIT'
    if re.search(r'文件过大|文件大小.*(超过|超出)|不支持.*格式|格式不支持|无效.*文件|文件格式.*错误',message):
        return 'PERMANENT_REJECT'
    if ACCOUNT_RESTRICTION.search(message):
        return 'ACCOUNT_RESTRICTED'
    if re.search(r'扫码验证|请.*(完成|进行).*(安全验证|人机验证|验证码|滑块)|请.*扫码.*验证',message):
        return 'VERIFICATION_REQUIRED'
    if re.search(r'(请求|操作|访问|发布).{0,8}(太|过于)?频繁|系统繁忙|服务器繁忙|服务繁忙|系统忙.{0,15}稍后',message):
        return 'PLATFORM_BUSY'
    if re.search(r'登录失效|登录过期|请重新登录|请先登录|请.*(完成|进行).*(安全验证|人机验证|验证码|滑块)|'
                 r'账号.*(封禁|异常|风险)|账号因违规操作已受到限制|无法访问创作者中心|操作频繁|访问受限',message):
        return 'AUTH_REQUIRED'
    if re.search(r'请.*(填写标题|添加标题|作品描述|选择.*声明|完成.*声明)',message):
        return 'CONFIG_REQUIRED'
    if re.search(r'上传失败|发布失败|转码失败|处理失败|网络异常|网络错误|提交失败',message):
        return 'FAILED'
    return ''


class CreatorStudioUploader(BrowserPublishWorkflow):
    stage_interruptions = (UploadStopped, PlatformRejected)
    title_selector = ''
    description_selector = ''
    combined_description = False

    def __init__(self,account,profile,report_root,stop_requested=lambda:False,
                 progress_callback=lambda message:None,headless=False):
        self.account,self.profile,self.report_root=account,Path(profile),Path(report_root)
        self.channel=CHANNELS[account.platform]
        self.stop_requested,self.log,self.headless=stop_requested,progress_callback,headless
        self.before_submit=lambda info:None
        self.before_upload=lambda:None
        self.supports_upload_boundary=True
        self.upload_seconds,self.publish_seconds,self.stable_seconds=1800,600,3
        self.last_outcome,self.submitted,self.stage='FAILED',False,'启动'
        self.title,self.file_name,self.confirmation='','',None
        self.platform_error,self.business_rejected=None,False
        self.busy_seen,self.prior_sources,self.completion_reset=False,set(),True
        self.file_selected=False
        self._upload_activity_seen=False
        self._last_upload_diagnostic=0
        self.control_seconds=30
        self.publish_details={}
        self.account_block=None

    def stop_check(self):
        if self.stop_requested():
            raise UploadStopped()

    def check_rejection(self,page):
        if self.platform_error and self.platform_error[0]!='FAILED':
            raise PlatformRejected(*self.platform_error)
        if self.confirmation and self.submitted:
            return
        address=urlsplit(page.url)
        if address.hostname!=self.channel.host or re.search(r'/(login|signin)(?:/|$)',address.path):
            raise PlatformRejected('AUTH_REQUIRED','平台登录页面或来源发生变化，请重新登录')
        messages=page.evaluate(CREATOR_NOTICES_JS)
        candidates=[self.platform_error] if self.platform_error else []
        for item in messages:
            state='VERIFICATION_REQUIRED' if item['verification'] else rejection(item['text'])
            # Static upload rules ("不得超过60分钟") are not an actual rejection.
            access_blocked=item['accessPage'] and state in ('AUTH_REQUIRED','ACCOUNT_RESTRICTED') and bool(re.search(
                r'账号(?:已|已经)?(?:被)?封禁|无法访问创作者中心|账号因违规操作已受到限制',item['text']))
            if state and (item['notice'] or state=='ACCOUNT_LIMIT' or access_blocked
                          or re.match(r'^(上传失败|发布失败|转码失败|处理失败|提交失败|请重新登录)',item['text'])):
                candidates.append((state,item['text']))
        if candidates:
            # A generic toast can appear before the actual security panel in DOM
            # order. Account-wide restrictions take priority over file failures.
            priority={'ACCOUNT_LIMIT':0,'ACCOUNT_RESTRICTED':1,'AUTH_REQUIRED':1,'VERIFICATION_REQUIRED':2,'PLATFORM_BUSY':3}
            state,message=min(candidates,key=lambda c:priority.get(c[0],4))
            if state in ('ACCOUNT_RESTRICTED','VERIFICATION_REQUIRED','PLATFORM_BUSY'):
                self.account_block={'status':state,'message':message}
                # A challenge alone does not prove that an already-clicked post
                # was rejected. Keep the account block attached to the failed file.
                self.business_rejected=self.business_rejected or any(s=='FAILED' for s,_ in candidates)
                self.log('[平台阻止] '+message+'；停止该账号后续文件与本轮重试；登录状态不变')
            raise PlatformRejected(state,message)

    def wait(self,page,label,condition,seconds=60):
        return self._wait_until(page,label,condition,seconds)

    def _check_stop(self):
        self.stop_check()

    def _progress(self,message):
        self.log(message)

    def _check_duration_notice(self,page):
        self.check_rejection(page)

    def _read_upload_state(self,page):
        state=self.read_upload_state(page)
        self.busy_seen=self.busy_seen or state['busy'] or bool(state['progress'])
        self.completion_reset=self.completion_reset or not state['complete']
        self._upload_activity_seen=self.busy_seen
        preview=any(v['ready']>=1 and v['src'] not in self.prior_sources for v in state['videos'])
        remote=any(v['ready']>=1 and v['src'].startswith(('https://','http://'))
                   and v['src'] not in self.prior_sources for v in state['videos'])
        evidence=(state['complete'] and (self.completion_reset or self.busy_seen)) or remote or (self.busy_seen and preview)
        selected=state['selected'] or (self.file_selected and evidence)
        form=bool(self.one_visible(page.locator(self.title_selector)))
        if time.monotonic()-self._last_upload_diagnostic>=30:
            submit=self.publish_control(page,inspect_only=True)
            blockers=[name for name,blocked in [('上传/处理仍在进行',state['busy']),('本次文件未确认',not selected),
                ('缺少本次完成证据',not evidence),('编辑区不可用',not form)] if blocked]
            self.log(f"[上传校验] 完成提示={state['complete']}；本次上传已观测={self.busy_seen}；"
                     f"视频元数据就绪={preview}；编辑区={form}；发布按钮={bool(submit)}；"
                     f"进度={state['progress']}；处理中={state['pending']}；"
                     f"等待原因={'、'.join(blockers) if blockers else '连续稳定检查'}")
            if not submit:
                self.log('[发布按钮属性] '+str(self.publish_details)+'；将在合集配置后校验')
            self._last_upload_diagnostic=time.monotonic()
        return {'ready':bool(evidence and selected and form),'pending':state['pending'] or (['平台上传/处理中'] if state['busy'] else []),
                'incomplete':state['busy'],'progress':[dict(p,upload=True) for p in state['progress']],
                'failed':[],'reupload':bool(selected and evidence),'form':form,'preview':preview}


    @staticmethod
    def one_visible(locator, *, enabled=True):
        items=[locator.nth(i) for i in range(locator.count()) if locator.nth(i).is_visible()]
        if len(items)>1:
            raise PlatformRejected('CONFIG_REQUIRED','存在多个匹配控件，请采集当前页面，未执行不确定的操作')
        if not items:
            return None
        item=items[0]
        if not enabled:
            return item
        interactive=item.evaluate(r'''e=>{
            for(let n=e;n&&n!==document.body;n=n.parentElement||n.getRootNode().host){
                if(n.hasAttribute('disabled')||n.getAttribute('aria-disabled')==='true'
                   || /(?:^|[\s_-])disabled(?:$|[\s_-])/i.test(n.className||'')) return false;
            }
            return getComputedStyle(e).pointerEvents!=='none';
        }''')
        return item if interactive and item.is_enabled() else None

    @staticmethod
    def control_details(control):
        details=control.evaluate(r'''e=>({tag:e.tagName.toLowerCase(),class:e.className,
            disabled:e.hasAttribute('disabled'),ariaDisabled:e.getAttribute('aria-disabled'),
            ariaBusy:e.getAttribute('aria-busy'),pointerEvents:getComputedStyle(e).pointerEvents,
            disabledAncestors:[...function*(n){
                for(;n&&n!==document.body;n=n.parentElement||n.getRootNode().host)yield n;
            }(e)].filter(n=>n.hasAttribute('disabled')||n.getAttribute('aria-disabled')==='true'
                ||/(?:^|[\s_-])disabled(?:$|[\s_-])/i.test(n.className||''))
                .map(n=>({tag:n.tagName.toLowerCase(),class:n.className})).slice(0,3)})''')
        details['nativeEnabled']=control.is_enabled()
        details['visible']=control.is_visible()
        return details

    @staticmethod
    def control_usable(details):
        return (details['visible'] and details['nativeEnabled'] and details['pointerEvents']!='none'
                and not details['disabledAncestors'] and details['ariaBusy']!='true')

    @staticmethod
    def click_owner(label):
        """Text matches may be pointer-events:none children of an actionable button."""
        distance=label.evaluate(r'''e=>{
            const semantic=e.closest('button,[role=button],[role=combobox],[role=option]');
            for(let n=e,i=0;n&&n!==document.body;n=n.parentElement,i++){
                if(semantic){if(n===semantic)return i;continue;}
                const s=getComputedStyle(n);
                if(s.pointerEvents!=='none' && (n.hasAttribute('onclick')
                    || /button|btn/i.test(n.className||'')
                    || s.cursor==='pointer' && (n.innerText||'').trim()===(e.innerText||'').trim()))return i;
            }
            return 0;
        }''')
        for _ in range(distance):
            label=label.locator('xpath=..')
        return label

    def clickable(self,locator):
        owners=[]
        for i in range(locator.count()):
            label=locator.nth(i)
            if not label.is_visible():
                continue
            owner=self.click_owner(label)
            if not any(owner.evaluate('(e,other)=>e===other',prior.element_handle()) for prior in owners):
                owners.append(owner)
        if len(owners)>1:
            raise PlatformRejected('CONFIG_REQUIRED','存在多个可见操作控件，未执行不确定的点击')
        return self.one_visible(owners[0]) if owners else None

    def publish_control(self,page, *, inspect_only=False):
        locator=page.get_by_role('button',name='发布',exact=True)
        if not any(locator.nth(i).is_visible() for i in range(locator.count())):
            locator=page.get_by_text('发布',exact=True)
        details=[]
        for i in range(locator.count()):
            label=locator.nth(i)
            if not label.is_visible():
                continue
            owner=self.click_owner(label)
            details.append(self.control_details(owner))
        self.publish_details={'matched':len(details),'owners':details}
        if inspect_only:
            return len(details)==1 and self.control_usable(details[0])
        control=self.clickable(locator)
        return control if control and self.control_usable(self.control_details(control)) else None

    def wait_publish_control(self,page):
        last_log=0
        def ready():
            nonlocal last_log
            control=self.publish_control(page)
            if not control and time.monotonic()-last_log>=10:
                self.log('[发布按钮校验] '+str(self.publish_details)+'；上传已就绪，配置后独立校验，不再按上传中等待')
                last_log=time.monotonic()
            return control
        try:
            return self.wait(page,'配置完成后校验实际发布按钮',ready,self.control_seconds)
        except RuntimeError as error:
            raise PlatformRejected('CONFIG_REQUIRED','上传已完成，但实际发布按钮仍不可用；未提交，请查看按钮属性及诊断报告') from error

    def attach_responses(self,page):
        submitted_requests=set()
        def requested(request):
            if self.submitted and request.method=='POST' and self.publish_endpoint(request.url):
                submitted_requests.add(request)
        page.on('request',requested)
        def received(response):
            if response.request not in submitted_requests or not self.submitted:
                return
            submitted_requests.discard(response.request)
            if not self.publish_endpoint(response.url):
                return
            try:
                body=response.json()
                if not isinstance(body,dict):
                    return
                message=str(body.get('message') or body.get('msg') or body.get('error_msg') or body.get('status_msg') or '')
                code=body.get('code',body.get('status_code',body.get('result')))
                accepted=body.get('success') is not False and (body.get('success') is True if code is None else (
                    str(code)=='1' if 'result' in body and 'code' not in body and 'status_code' not in body else str(code)=='0'))
                self.log(f'[发布接口] {safe_url(response.url)}；HTTP {response.status}；业务码 {code}；{message[:180]}')
                state=rejection(message)
                if response.status==429:
                    state,message='PLATFORM_BUSY',message or '平台请求过于频繁（HTTP 429），请稍后再试'
                if state and (not accepted or response.status>=400):
                    self.business_rejected=True
                    self.platform_error=(state,message)
                    if state in ('ACCOUNT_RESTRICTED','VERIFICATION_REQUIRED','PLATFORM_BUSY'):
                        self.account_block={'status':state,'message':message}
                    return
                if 200<=response.status<300 and accepted:
                    identifiers=[]
                    def ids(data,depth=0):
                        if not isinstance(data,dict) or depth>3:
                            return
                        for key,value in data.items():
                            if key in ('note_id','noteId','photo_id','photoId','work_id','video_id') and isinstance(value,(str,int)) and str(value):
                                if not isinstance(value,bool):
                                    identifiers.append((key,str(value)))
                            elif key in ('data','result','note','photo','video'):
                                ids(value,depth+1)
                    ids(body)
                    preferred=('note_id','noteId') if self.account.platform=='xiaohongshu' else ('photo_id','photoId','work_id')
                    post_ids={value for key,value in identifiers if key in preferred}
                    post_ids=post_ids or {value for _,value in identifiers}
                    if len(post_ids)<=1:
                        # An accepted response to this exact publish request is
                        # proof even when this API version omits the note ID.
                        self.confirmation={'kind':'publish_response','endpoint':safe_url(response.url),
                            'accepted':True,'business_code':code}
                        if post_ids:
                            self.confirmation['post_id']=next(iter(post_ids))
                        self.before_submit(self.result_hint())
                elif (code is not None or body.get('success') is False) and message:
                    self.business_rejected=True
                    self.platform_error=('FAILED',message)
            except Exception:
                pass  # Unreadable responses never confirm publication.
        page.on('response',received)

    def publish_endpoint(self,url):
        address=urlsplit(url)
        if address.scheme!='https' or address.hostname not in ('creator.xiaohongshu.com','edith.xiaohongshu.com'):
            return False
        path=address.path.lower().rstrip('/')
        return path=='/web_api/sns/v2/note' or bool(re.search(r'/(?:publish|submit|release)$',path)
            and not re.search(r'upload|draft|precheck|check|limit|metric|log|status|list|detail|refresh',path))

    def verify_identity(self,page):
        wanted=self.account.platform_nickname.strip()
        if not wanted:
            raise PlatformRejected('CONFIG_REQUIRED','请先使用“登录 / 获取平台昵称”绑定实际账号昵称')
        identity=self.wait(page,'核对实际平台账号',lambda:extract_platform_identity(page,self.account.platform),30)
        if identity['nickname']!=wanted or (self.account.platform_uid and identity.get('platform_uid')
                                           and self.account.platform_uid!=identity['platform_uid']):
            raise PlatformRejected('IDENTITY_MISMATCH',f'实际账号 {identity["nickname"]} 与绑定账号 {wanted} 不一致，未上传文件')
        self.log(f'[账号核对] {self.channel.name} / {wanted}；使用该账号独立浏览器目录')

    def video_input(self,page):
        switched=False
        def find():
            nonlocal switched
            candidates=page.locator(file_input_selector(self.account.platform))
            matches=[candidates.nth(i) for i in range(candidates.count())
                     if re.search(r'video|\.mp4|\.mov',candidates.nth(i).get_attribute('accept') or '',re.I)]
            if len(matches)>1:
                raise PlatformRejected('CONFIG_REQUIRED','视频文件入口不唯一，请采集当前页面')
            if matches:
                return matches[0]
            if self.account.platform=='xiaohongshu' and not switched:
                tabs=page.locator('.creator-tab').filter(has_text=re.compile(r'^上传视频$'))
                if not tabs.count():
                    tabs=page.get_by_role('tab',name='上传视频',exact=True)
                visible=[tabs.nth(i) for i in range(tabs.count()) if tabs.nth(i).is_visible()]
                active=[tab for tab in visible if tab.evaluate("e=>e.classList.contains('active')||e.getAttribute('aria-selected')==='true'")]
                if active:
                    # The active tab can be rendered twice by the responsive shell.
                    # No click is necessary; wait for its asynchronous file input.
                    return None
                if len(visible)>1:
                    raise PlatformRejected('CONFIG_REQUIRED','视频切换标签不唯一，未执行不确定的点击')
                if visible:
                    self.stop_check()
                    visible[0].click(timeout=1000)
                    switched=True
            return None
        return self.wait(page,'等待视频上传入口',find)

    def fill_details(self,page,video):
        self.title=video.stem if self.account.title_mode=='filename' else resolve_publish_series(video.name)
        title=self.wait(page,'等待作品编辑区',lambda:self.one_visible(page.locator(self.title_selector)),self.upload_seconds)
        content=self.title+('\n'+self.account.description if self.combined_description and self.account.description else '')
        limit=title.get_attribute('maxlength')
        if limit and limit.isdigit() and len(content)>int(limit):
            raise PlatformRejected('CONFIG_REQUIRED',f'标题/作品描述超过当前页面限制 {limit} 字，请调整标题规则')
        self.stop_check()
        title.fill(content,timeout=1500)
        def text(control):
            return control.evaluate("e=>e.matches('input,textarea')?e.value:(e.innerText||'')").strip()
        self.wait(page,'核对作品标题/描述',lambda:text(title)==content.strip(),5)
        if self.description_selector:
            description=self.wait(page,'等待正文编辑区',lambda:self.one_visible(page.locator(self.description_selector)))
            self.stop_check()
            description.fill(self.account.description,timeout=1500)
            self.wait(page,'核对正文内容',lambda:text(description)==self.account.description.strip(),5)
        self.log('[内容填写] 标题：'+self.title+'；正文'+('使用已配置内容' if self.account.description else '留空，不从文件名生成正文'))

    def read_upload_state(self,page):
        return page.evaluate(r'''({filename,platform})=> {
            const visible=e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0&&getComputedStyle(e).visibility!=='hidden'};
            // Status labels often contain an SVG/thumb icon plus a direct text node.
            // A leaf-only scan misses these, even though the label is visible to the user.
            const leaves=[...document.querySelectorAll('*')].filter(e=>visible(e)
                &&!e.closest('input,textarea,[contenteditable=true]')).map(e=>!e.children.length
                    ?(e.innerText||'').trim():[...e.childNodes].filter(n=>n.nodeType===Node.TEXT_NODE)
                        .map(n=>n.textContent).join('').trim()).filter(Boolean);
            const progress=[...document.querySelectorAll('[role=progressbar],progress,[class*="upload-progress"],[class*="uploadProgress"]')]
                .filter(visible).map(e=>({value:Number(e.getAttribute('aria-valuenow')??e.getAttribute('value')??-1),
                    max:Number(e.getAttribute('aria-valuemax')??e.getAttribute('max')??100)}));
            const busy=leaves.some(t=>/^(正在上传|上传中|正在转码|转码中|视频处理中|正在处理|处理中)/.test(t)
                || /^(?:\d+(?:\.\d+)?)\s*%$/.test(t)&&parseFloat(t)<100)
                || progress.some(p=>p.value>=0&&p.value<p.max)
                || [...document.querySelectorAll('[aria-busy=true]')].some(visible);
            const complete=leaves.some(t=>/^(视频)?上传(?:成功|完成|完毕)|^(视频)?(?:处理完成|转码完成)/.test(t)
                || platform==='xiaohongshu'&&/^检测为高清视频/.test(t.replace(/^[^\p{L}\p{N}]+/u,'')));
            const videos=[...document.querySelectorAll('video')].filter(visible).map(e=>({src:e.currentSrc||e.src,ready:e.readyState}));
            const selected=[...document.querySelectorAll('input[type=file]')].some(e=>[...(e.files||[])].some(f=>f.name===filename))
                || leaves.some(t=>t.includes(filename));
            return {busy,complete,progress,videos,selected,
                pending:leaves.filter(t=>/^(正在上传|上传中|正在转码|转码中|视频处理中|正在处理|处理中)/.test(t)).slice(0,4)};
        }''',{'filename':self.file_name,'platform':self.account.platform})

    def wait_upload(self,page):
        self._wait_for_upload_ready(page,self.upload_seconds,ready_stable_seconds=self.stable_seconds)
        self.log('[上传完成] 本次文件已就绪，上传/转码状态已消失，进入合集及发布配置')

    def set_cover(self,page):
        self.log('[封面] 使用平台从本次视频生成的默认首帧封面，不点击 AI 推荐封面')


    def select_collection(self,page):
        pass  # Platforms without an implemented collection control keep their current settings.

    def confirm_publish(self,page):
        def confirmed():
            self.check_rejection(page)
            if self.confirmation:
                return self.confirmation
            notice=self.success_notice(page)
            if notice:
                self.confirmation={'kind':'success_notice','text':notice.inner_text()[:200]}
                self.before_submit(self.result_hint())
                return self.confirmation
            return None  # Redirects, titles and previews are not publication proof.
        self.stage='等待平台确认本次发布（最长10分钟）'
        self.log(self.stage)
        result=self._wait_for_confirmation(page,confirmed,self.publish_seconds,poll_ms=300)
        if not result:
            raise RuntimeError('等待平台确认本次发布超时；未确认成功按失败处理，原文件保留')
        return result

    def success_notice(self,page):
        notices=page.get_by_text(SUCCESS_NOTICE)
        for index in range(notices.count()):
            notice=notices.nth(index)
            if not notice.is_visible() or notice.evaluate("e=>!!e.closest('input,textarea,[contenteditable=true]')"):
                continue
            if SUCCESS_NOTICE.fullmatch(self.title) and not notice.evaluate("e=>!!e.closest('[role=alert],[class*=toast],[class*=success]')"):
                continue
            return notice
        return None

    def result_hint(self):
        return {'platform':self.account.platform,'account_id':self.account.account_id,
                'nickname':self.account.platform_nickname,'file_name':self.file_name,'title':self.title,
                'confirmation':self.confirmation,'account_block':self.account_block,'submitted':self.submitted}

    def reconcile(self,info):
        """Read captured, account-bound API acknowledgement; titles alone prove nothing."""
        if (not isinstance(info,dict) or info.get('platform')!=self.account.platform
                or info.get('account_id')!=self.account.account_id or info.get('submitted') is False):
            return False
        confirmation=info.get('confirmation')
        if not isinstance(confirmation,dict):
            return False
        if confirmation.get('kind')=='success_notice':
            return bool(info.get('submitted') is True and SUCCESS_NOTICE.fullmatch(str(confirmation.get('text',''))))
        if confirmation.get('kind')!='publish_response':
            return False
        endpoint=urlsplit(confirmation.get('endpoint',''))
        if self.account.platform=='kuaishou':
            return (endpoint.scheme=='https' and endpoint.hostname==self.channel.host
                and confirmation.get('accepted') is True and confirmation.get('business_code')==1)
        return bool(confirmation.get('accepted') is not False and self.publish_endpoint(confirmation.get('endpoint','')) and (
            confirmation.get('post_id') or confirmation.get('accepted') is True
            and (confirmation.get('business_code') is None or str(confirmation.get('business_code')) in ('0','1'))))

    def upload_single_video(self,video):
        video=Path(video)
        self.last_outcome,self.submitted,self.confirmation,self.platform_error='FAILED',False,None,None
        self.file_name,self.title,self.busy_seen,self.business_rejected=video.name,'',False,False
        self.file_selected=False
        self.account_block=None
        self._video_selected,self._publish_armed=False,False
        self._upload_activity_seen,self._last_upload_diagnostic=False,0
        context,page=None,None
        try:
            self.stop_check()
            if media_type(video)!='video':
                raise PlatformRejected('PERMANENT_REJECT','当前自动发布器仅支持视频；独立音频/图片不上传、不重试')
            state=account_dom_state(self.report_root,self.account.platform,self.account.account_id)
            if not structure_ready(state):
                raise PlatformRejected('CONFIG_REQUIRED',state.message+'；请先采集该账号的视频发布编辑页')
            self.profile.mkdir(parents=True,exist_ok=True)
            with sync_playwright() as p:
                try:
                    self.log('[启动浏览器] '+self.channel.name)
                    from .media_browser import launch_media_context
                    context=launch_media_context(p,str(self.profile),headless=self.headless,
                        viewport={'width':1440,'height':900},timeout=10000)
                    context.set_default_timeout(1000)
                    page=context.pages[0] if context.pages else context.new_page()
                    self.attach_responses(page)
                    page.goto(self.channel.url,wait_until='domcontentloaded',timeout=45000)
                    self.verify_identity(page)
                    file_input=self.video_input(page)
                    prior=self.read_upload_state(page)
                    self.prior_sources={v['src'] for v in prior['videos']}
                    self.busy_seen,self.completion_reset=False,not prior['complete']
                    self.stop_check()
                    self.before_upload()
                    file_input.set_input_files(str(video.resolve()),timeout=10000)
                    self.file_selected,self._video_selected=True,True
                    self.log('[选择视频] '+video.name)
                    submit=self.prepare_publish_form(page,
                        details=('填写作品标题与正文',lambda:self.fill_details(page,video)),
                        cover=('确认平台默认视频封面',lambda:self.set_cover(page)),
                        collection=('配置平台合集',lambda:self.select_collection(page)),
                        upload=('等待本次视频上传、处理及封面就绪（最长30分钟）',lambda:self.wait_upload(page)),
                        submit=('配置完成后校验实际发布按钮',lambda:self.wait_publish_control(page)),
                        uploaded_before_options=True)
                    if self.success_notice(page):
                        raise PlatformRejected('CONFIG_REQUIRED','页面已有发布成功提示，无法归属本次文件，未提交')
                    self.stop_check()
                    self.before_submit(self.result_hint())
                    self.stop_check()
                    self.submitted,self._publish_armed=True,True
                    submit.click(timeout=1500)
                    self.log('[已点击发布] 保持浏览器打开，等待本次发布成功证据')
                    self.confirmation=self.confirm_publish(page)
                    self.last_outcome='SUCCESS'
                    self.log('[发布确认] 平台已明确确认本次投稿成功')
                    return True,'平台已确认发布成功，等待统一归档',self.result_hint()
                except Exception as failure:
                    if not self.confirmation:
                        self._capture_failure_page(page, failure)
                    if not isinstance(failure,UploadStopped) and page and not page.is_closed():
                        try:
                            report,_=ChannelDOMProbe(self.account,self.report_root).capture(page,'异常提示')
                            self.log(f'[诊断] 阶段 {self.stage}：{report}')
                        except Exception as error:
                            self.log('[诊断保存失败] '+str(error)[:200])
                    raise
                finally:
                    if context:
                        try:
                            self._close_publish_context(context,page,self.submitted)
                        except Exception:
                            pass
        except UploadStopped:
            if self.confirmation:
                self.last_outcome='SUCCESS'
                return True,'平台已确认发布成功；保留停止前的成功回执',self.result_hint()
            self.last_outcome='FAILED' if self._video_selected else 'CANCELLED'
            return False,'用户停止；未确认成功按失败处理' if self.submitted else '用户停止；未提交发布',self.result_hint()
        except PlatformRejected as error:
            if self.confirmation:
                self.last_outcome='SUCCESS'
                return True,'平台已确认发布成功；保留有效发布回执',self.result_hint()
            certain=error.status in ('ACCOUNT_LIMIT','DURATION_LIMIT','PERMANENT_REJECT','CONFIG_REQUIRED') or self.business_rejected or bool(re.search(r'发布失败|提交失败',str(error)))
            self.business_rejected = bool(certain)
            self.last_outcome='FAILED' if self.submitted and not certain else error.status
            return False,str(error),self.result_hint()
        except Exception as error:
            if self.confirmation:
                self.last_outcome='SUCCESS'
                return True,'平台已确认发布成功；关闭页面异常不改变发布结果',self.result_hint()
            self.last_outcome='FAILED'
            return False,str(error),self.result_hint()


class XiaohongshuUploader(CreatorStudioUploader):
    title_selector='input[placeholder="填写标题会有更多赞哦"]'
    description_selector='[contenteditable=true].tiptap[role=textbox]'

    def publish_control(self,page, *, inspect_only=False):
        # The site's current component exposes its closed root as _sr. Its
        # actual native button supports Playwright's normal actionability checks.
        hosts=page.locator('xhs-publish-btn:visible')
        count=hosts.count()
        if not count:
            return super().publish_control(page,inspect_only=inspect_only)
        self.publish_details={'matched':0,'owners':[],'component':'xhs-publish-btn','hosts':count}
        if count!=1:
            if inspect_only:
                return False
            raise PlatformRejected('CONFIG_REQUIRED','存在多个小红书发布组件，未执行不确定的点击')
        host=hosts.first
        state=host.evaluate('''e=>({isPublish:e.getAttribute('is-publish'),
            submitText:e.getAttribute('submit-text'),disabled:e.getAttribute('submit-disabled'),
            loading:e.getAttribute('submit-loading'),rootMode:e._sr?.mode||null,
            buttons:e._sr instanceof ShadowRoot&&e._sr.host===e
                ?[...e._sr.querySelectorAll('button')].filter(b=>(b.innerText||'').trim()==='发布').length:0})''')
        self.publish_details['host']=state
        if state['buttons']>1:
            if inspect_only:
                return False
            raise PlatformRejected('CONFIG_REQUIRED','小红书组件内发布按钮不唯一，未提交')
        host_blocked=(state['isPublish']=='false' or state['submitText'] not in (None,'发布')
                      or state['disabled'] not in (None,'false') or state['loading'] not in (None,'false'))
        if state['buttons']!=1:
            return False if inspect_only else None
        handle=host.evaluate_handle('''e=>e._sr instanceof ShadowRoot&&e._sr.host===e
            ?[...e._sr.querySelectorAll('button')].find(b=>(b.innerText||'').trim()==='发布')||null:null''')
        button=handle.as_element()
        if button is None:
            handle.dispose()
            return False if inspect_only else None
        details=self.control_details(button)
        self.publish_details.update(matched=1,owners=[details])
        usable=self.control_usable(details) and not host_blocked
        if inspect_only or not usable:
            handle.dispose()
            return usable if inspect_only else None
        return button

    def select_collection(self,page):
        target=self.account.collection_name.strip() or resolve_publish_series(self.file_name)
        self.stage='选择小红书合集：'+target
        self.log(self.stage)
        heading=self.one_visible(page.get_by_text('加入合集',exact=True),enabled=False)
        if heading is None:
            raise PlatformRejected('CONFIG_REQUIRED','未找到“加入合集”区域，未点击发布；请采集内容设置区域')
        # Keep the selected-value check inside this row, never the note title or phone preview.
        row=heading
        control=None
        def selected_in_row():
            matches=row.get_by_text(target,exact=True)
            selected=[]
            for i in range(matches.count()):
                item=matches.nth(i)
                if item.is_visible() and not item.evaluate('''e=>!!e.closest(
                    '[role=option],[role=listbox],[role=dialog],[class*="menu" i],'
                    +'[class*="popover" i],[class*="popper" i],[class*="option" i]')'''):
                    selected.append(item)
            if len(selected)>1:
                raise PlatformRejected('CONFIG_REQUIRED','加入合集区域的选中名称不唯一，未点击发布')
            return selected[0] if selected else None
        for _ in range(5):
            row=row.locator('xpath=..')
            if row.evaluate("e=>e.matches('body,html')||!!e.querySelector('input[placeholder=\"填写标题会有更多赞哦\"]')"):
                break
            current=selected_in_row()
            if current:
                self.log('[合集确认] '+target+'；已在加入合集区域选中')
                return
            control=self.clickable(row.get_by_text('选择合集',exact=True))
            if control is None:
                control=self.clickable(row.get_by_role('combobox'))
            if control is None:
                control=self.clickable(row.locator('button,[aria-haspopup=listbox]'))
            if control:
                break
        if control is None:
            raise PlatformRejected('CONFIG_REQUIRED','未找到唯一合集选择入口，未点击发布')
        self.stop_check()
        control.click(timeout=1500)
        def find_option():
            option=self.clickable(page.get_by_role('option',name=target,exact=True))
            if option:
                return option
            candidates=page.get_by_text(target,exact=True)
            usable=[]
            for i in range(candidates.count()):
                candidate=candidates.nth(i)
                if candidate.is_visible() and candidate.evaluate('''e=>!e.closest('input,textarea,[contenteditable=true]')
                    &&!!e.closest('[role=listbox],[role=dialog],[class*="dropdown" i],[class*="popover" i],'
                        +'[class*="popper" i],[class*="option" i],[class*="menu" i]')'''):
                    usable.append(candidate)
            if len(usable)>1:
                raise PlatformRejected('CONFIG_REQUIRED','合集选项名称重复，未执行不确定的选择')
            return self.one_visible(self.click_owner(usable[0])) if usable else None
        try:
            option=self.wait(page,'查找小红书合集：'+target,find_option,15)
            self.stop_check()
            option.click(timeout=1500)
            self.wait(page,'核对小红书合集：'+target,
                      selected_in_row,10)
        except (RuntimeError,PlaywrightTimeoutError) as error:
            raise PlatformRejected('CONFIG_REQUIRED',f'合集“{target}”未找到或选择后回读失败，未点击发布；请检查合集名称并采集下拉选项') from error
        self.log('[合集确认] '+target+'；已回读加入合集区域')


class KuaishouUploader(CreatorStudioUploader):
    title_selector='#work-description-edit[contenteditable=true]'
    combined_description=True

    def attach_responses(self,page):
        # The official video publisher treats result=1 from pc/submit as
        # accepted publication, then redirects to management. No work ID is
        # required by that contract; publish/refresh is only a status poll.
        # Bind the acknowledgement to a request created AFTER our click is
        # armed, so a late response from an earlier page cannot confirm it.
        requests=set()
        endpoints={f'/rest/cp/works/{version}/video/pc/{action}'
                   for version in ('v2','v3') for action in ('submit','isolation/submit')}

        def eligible(url,method):
            address=urlsplit(url)
            return (address.hostname=='cp.kuaishou.com' and address.scheme=='https'
                    and address.path in endpoints and method=='POST')

        def started(request):
            if self.submitted and eligible(request.url,request.method):
                requests.add(request)

        def received(response):
            request=response.request
            if not self.submitted or request not in requests:
                return
            requests.discard(request)
            if not eligible(request.url,request.method) or not eligible(response.url,request.method):
                return
            try:
                body=response.json()
                if not isinstance(body,dict):
                    return
                code=body.get('result')
                message=str(body.get('message') or body.get('msg') or '')
                accepted=(200<=response.status<300 and not isinstance(code,bool)
                          and str(code)=='1' and body.get('success') is not False
                          and 'code' not in body and 'status_code' not in body)
                self.log(f'[发布提交接口] {safe_url(response.url)}；HTTP {response.status}；业务码 {code}；{message[:180]}')
                if accepted:
                    self.confirmation={'kind':'publish_response','endpoint':safe_url(response.url),
                                       'business_code':1,'accepted':True}
                    self.before_submit(self.result_hint())
                    self.log('[提交确认] 快手已确认接收本次作品；后续审核/转码由平台处理')
                    return
                state=rejection(message)
                if response.status==429:
                    state,message='PLATFORM_BUSY',message or '平台请求过于频繁（HTTP 429），请稍后再试'
                if state or (code is not None and not isinstance(code,bool) and str(code)!='1' and message):
                    state=state or 'FAILED'
                    self.business_rejected=True
                    self.platform_error=(state,message)
                    if state in ('ACCOUNT_RESTRICTED','VERIFICATION_REQUIRED','PLATFORM_BUSY'):
                        self.account_block={'status':state,'message':message}
            except Exception:
                pass  # Missing/unreadable acknowledgements remain unconfirmed.

        page.on('request',started)
        page.on('response',received)
