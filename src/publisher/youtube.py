"""YouTube Studio publisher. Upload, draft and persisted visibility are separate states."""
import re
import time
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from .browser_workflow import BrowserPublishWorkflow
from .channel_probe import ChannelDOMProbe
from .douyin.uploader import resolve_publish_series
from .titles import resolve_title
from .youtube_browser import launch_youtube_context, google_login_rejected, GOOGLE_LOGIN_HELP, youtube_runtime_summary


class UploadStopped(Exception):
    pass


class PlatformRejected(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def youtube_video_id(url):
    parsed = urlsplit(url)
    if parsed.hostname not in ('youtu.be', 'youtube.com', 'www.youtube.com', 'studio.youtube.com'):
        return ''
    candidate = (parsed.path.strip('/') if parsed.hostname == 'youtu.be' else
                 parse_qs(parsed.query).get('v', [''])[0])
    match = re.search(r'/(?:shorts|video)/([\w-]{11})(?:/|$)', parsed.path)
    candidate = match.group(1) if match else candidate
    return candidate if re.fullmatch(r'[\w-]{11}', candidate or '') else ''


class YouTubeUploader(BrowserPublishWorkflow):
    stage_interruptions = (UploadStopped, PlatformRejected)
    def __init__(self, account, profile, report_root, stop_requested=lambda: False,
                 progress_callback=lambda message: None, headless=False):
        self.account, self.profile, self.report_root = account, Path(profile), Path(report_root)
        self.stop_requested, self.log, self.headless = stop_requested, progress_callback, headless
        self.last_outcome = 'FAILED'
        self.submitted = False
        self.video_id = ''
        self.is_short = False
        self.title = ''
        self.stage = '启动'
        self.before_submit = lambda info: None
        self.before_upload = lambda: None
        self.supports_upload_boundary = True
        self._upload_activity_seen = False
        self.resume_info = None
        self.on_draft_identified = lambda info: None
        self._checkpoint_video_id = ''

    def stop_check(self):
        if self.stop_requested():
            raise UploadStopped()

    def pause(self,page,milliseconds=300):
        self._pause(page,milliseconds)
        self.check_rejection(page)

    def _check_stop(self):
        self.stop_check()

    def _progress(self,message):
        self.log(message)

    def attach_network_diagnostics(self,page):
        self.network_errors=[]
        seen=set()
        def record(kind,url,reason):
            parsed=urlsplit(url)
            if not parsed.hostname or not any(part in parsed.hostname for part in ('youtube','google','ytimg','ggpht')):
                return
            endpoint=parsed.scheme+'://'+parsed.hostname+parsed.path
            item=(endpoint,str(reason))
            if item in seen or len(seen)>=40:return
            seen.add(item)
            message=f'[网络{kind}] {endpoint}：{reason}'
            self.network_errors.append(message)
            self.log(message)
        page.on('requestfailed',lambda request:record('失败',request.url,request.failure))
        page.on('response',lambda response:record('响应异常',response.url,'HTTP '+str(response.status))
                if response.status>=400 else None)
        page.on('pageerror',lambda error:self.log('[页面脚本异常] '+str(error)[:500]))
        console_errors=set()
        def console(message):
            if message.type=='error' and message.text not in console_errors and len(console_errors)<20:
                console_errors.add(message.text)
                self.log('[浏览器控制台错误] '+message.text[:700])
        page.on('console',console)

    def open_saved_draft(self,page,info):
        identity=info.get('video_id','')
        if not re.fullmatch(r'[\w-]{11}',identity) or info.get('channel_id')!=self.account.channel_id:
            raise PlatformRejected('CONFIG_REQUIRED','旧草稿身份不完整；未重新上传，请先核验已有草稿')
        self.video_id=identity
        self.is_short=bool(info.get('is_short'))
        self.log('[恢复草稿] 正在打开已记录视频ID '+identity+'；不会重新选择或上传文件')
        page.goto(f'https://studio.youtube.com/video/{identity}/edit',wait_until='domcontentloaded',timeout=45000)
        def editor():
            dialog=self.one_visible(self.dialog(page))
            if dialog is not None:return dialog
            button=self.one_visible(page.get_by_role('button',name=re.compile(r'^(编辑草稿|继续编辑|Edit draft|Continue editing)$',re.I)))
            if button is not None:button.click(timeout=1000)
            return None
        self.wait(page,'等待原草稿编辑窗口（不新建视频）',editor,seconds=90)
        self._video_selected=True
        self._checkpoint_video_id=identity
        self.on_draft_identified(self.result_hint())

    def _check_duration_notice(self,page):
        self.check_rejection(page)

    def _read_upload_state(self,page):
        dialog=self.dialog(page)
        if not dialog.count() or not dialog.is_visible():
            return {'ready':False,'pending':['等待上传编辑区'],'incomplete':True,
                    'progress':[],'failed':[],'reupload':False,'form':False,'preview':False}
        state=self.read_upload_state(page)
        self._upload_activity_seen=self._upload_activity_seen or state['busy']
        for link in state['links']:
            candidate=youtube_video_id(link)
            if candidate:
                self.video_id=candidate
                self.is_short='/shorts/' in urlsplit(link).path
        if self.video_id and self.video_id != self._checkpoint_video_id:
            self.on_draft_identified(self.result_hint())
            self._checkpoint_video_id = self.video_id
        return {'ready':state['complete'] and bool(self.video_id),'pending':['上传/处理进行中'] if state['busy'] else [],
                'incomplete':state['busy'],'progress':[],'failed':[],
                'reupload':False,'form':bool(self.video_id),'preview':state['video']}


    def check_rejection(self, page):
        for status, pattern in [
            ('ACCOUNT_LIMIT', r'每日上传数量限制|每日上传上限|今天.*上传.*上限|Daily upload limit'),
            ('PERMANENT_REJECT', r'视频过长|文件过大|无效的文件格式|不支持.*文件格式|Video too long|Invalid file format'),
            ('FAILED', r'上传失败|处理失败|无法处理|Upload failed|Processing failed'),
        ]:
            notices = page.get_by_text(re.compile(pattern, re.I))
            for index in range(notices.count()):
                item = notices.nth(index)
                if item.is_visible():
                    raise PlatformRejected(status, item.inner_text()[:300])

    def wait(self,page,label,condition,seconds=60):
        return self._wait_until(page,label,condition,seconds)


    @staticmethod
    def one_visible(locator):
        visible = [locator.nth(i) for i in range(locator.count()) if locator.nth(i).is_visible()]
        if len(visible) > 1:
            raise RuntimeError('存在多个匹配控件，停止以避免操作错误')
        return visible[0] if visible and visible[0].is_enabled() else None

    def click(self, page, locator, label):
        def action():
            button = self.one_visible(locator)
            if button is None:
                return None
            self.stop_check()
            # Transient Studio scrims can cover an enabled, visible control.
            # Let the shared wait retry actionability timeouts; never force-click.
            button.click(timeout=1000)
            return True
        self.wait(page, label, action)

    def verify_channel(self, page, seconds=30):
        wanted = self.account.channel_id
        if not re.fullmatch(r'UC[\w-]{22}', wanted):
            raise PlatformRejected('IDENTITY_MISMATCH', '请先绑定有效YouTube频道ID（UC开头）')
        def check():
            parsed = urlsplit(page.url)
            if parsed.hostname == 'accounts.google.com':
                raise PlatformRejected('AUTH_REQUIRED', GOOGLE_LOGIN_HELP if google_login_rejected(page.url)
                    else 'Google登录已失效，请先在本账号的登录窗口使用“手工登录 Google”。')
            match = re.match(r'/channel/(UC[\w-]{22})(?:/|$)', parsed.path)
            if parsed.hostname == 'studio.youtube.com' and match:
                if match.group(1) != wanted:
                    raise PlatformRejected('IDENTITY_MISMATCH', f'实际频道{match.group(1)}与配置{wanted}不一致，未上传文件')
                return True
            return False
        self.wait(page, '核对实际频道ID', check, seconds)
        self.log(f'[频道核对] 已匹配 {wanted}；账号备注：{self.account.account_name}')

    def open_upload(self, page):
        self.click(page, page.get_by_role('button', name='创建', exact=True), '打开创建菜单')
        self.click(page, page.get_by_text('上传视频', exact=True), '打开上传视频窗口')
        def file_input():
            from .page_structure import file_input_selector
            candidates = page.locator(file_input_selector('youtube'))
            allowed = [candidates.nth(i) for i in range(candidates.count())
                       if not re.search(r'image|png|jpg|jpeg', candidates.nth(i).get_attribute('accept') or '', re.I)]
            if len(allowed) != 1:
                return None
            return allowed[0]
        return self.wait(page, '等待视频文件输入框', file_input)

    def dialog(self, page):
        return page.locator('ytcp-uploads-dialog [role=dialog]:visible').first

    def fill_details(self, page, video):
        title = resolve_title(self.account, video.name)
        title = title[:100]
        dialog = self.dialog(page)
        # Both editors share id=textbox. Scope to the active upload dialog and accessible label.
        title_input = dialog.locator('[contenteditable=true][role=textbox][aria-label*="标题"]')
        editor = self.wait(page, '等待标题编辑器', lambda: self.one_visible(title_input), 1800)
        editor.fill(title, timeout=1000)
        editor.press('Tab')
        if editor.inner_text().strip() != title:
            raise RuntimeError('标题回读不一致')
        description = dialog.locator('[contenteditable=true][role=textbox][aria-label*="介绍"]')
        if not description.count():
            description = dialog.locator('[contenteditable=true][role=textbox][aria-label*="说明"]')
        body = self.one_visible(description)
        if body is None:
            raise RuntimeError('未找到唯一说明编辑器')
        body.fill(self.account.description, timeout=1000)
        body.press('Tab')
        self.log(f'[标题] {title}；说明按配置填写')
        if self.account.made_for_kids is None:
            raise RuntimeError('请先配置视频是否面向儿童，不能自动推断')
        audience = '是，内容是面向儿童的' if self.account.made_for_kids else '不，内容不是面向儿童的'
        self.click(page, dialog.get_by_role('radio', name=re.compile('^' + re.escape(audience))), '设置观众')
        self.apply_default_details(page,dialog)
        self.title = title
        return title

    def apply_default_details(self,page,dialog):
        paid=dialog.get_by_role('radio',name=re.compile(r'^否.*(?:付费宣传|付费推广)'))
        if self.one_visible(paid) is None:
            def advanced_control():
                expand=dialog.get_by_role('button',name=re.compile(r'^(展开|显示更多|显示高级设置|Show more|Show advanced settings)$',re.I))
                control=self.one_visible(expand)
                if control is None:
                    control=self.one_visible(dialog.locator('ytcp-button#toggle-button').filter(
                        has_text=re.compile(r'^(展开|显示更多|Show more)$',re.I)))
                return control
            def expanded():
                # A Studio click may already expand the panel before Playwright
                # finishes its actionability wait. Re-check the resulting state
                # on every retry rather than waiting on a stale "Show more" label.
                ready=self.one_visible(paid)
                if ready is not None:return ready
                button=advanced_control()
                if button is not None:
                    self.stop_check()
                    button.click(timeout=1000)
                return self.one_visible(paid)
            self.wait(page,'展开并核验YouTube高级设置',expanded,seconds=90)
        self.log('[高级设置确认] 当前上传弹窗的付费宣传选项已就绪，不重复切换展开状态')
        self.click(page,paid,'默认不包含付费宣传')
        if paid.get_attribute('aria-checked')!='true' and not paid.evaluate('(el)=>Boolean(el.checked)'):
            raise RuntimeError('无付费宣传设置回读失败，未提交')
        def section(heading):
            label=dialog.get_by_text(heading,exact=True)
            return label.locator('xpath=ancestor::*[.//*[@role="radio"] or .//tp-yt-paper-radio-button][1]')
        ai=section('AI 使用情况')
        if not ai.count():
            ai=section('更改的内容')
        choice='是' if self.account.altered_content is not False else '否'
        control=ai.get_by_role('radio',name=choice,exact=True)
        if not control.count():
            control=dialog.get_by_role('radio',name='是，使用了 AI' if choice=='是' else '否，未使用 AI',exact=True)
        self.click(page,control,'设置AI使用情况：'+choice)
        if control.get_attribute('aria-checked')!='true' and not control.evaluate('(el)=>Boolean(el.checked)'):
            raise RuntimeError('AI使用情况回读失败，未提交')
        location=dialog.get_by_role('checkbox',name=re.compile(r'^允许自动显示地点信息'))
        checkbox=self.wait(page,'默认关闭自动地点信息',lambda:self.one_visible(location))
        if checkbox.get_attribute('aria-checked')=='true' or checkbox.evaluate('(el)=>Boolean(el.checked)'):
            host=checkbox.locator('xpath=ancestor::ytcp-checkbox-lit[1]')
            # Studio exposes the checkbox state on a pointer-events:none child;
            # its enclosing custom element owns the user click.
            target=self.one_visible(host) if host.count() else checkbox
            if target is None:
                raise RuntimeError('自动地点信息缺少唯一操作入口，未提交')
            target.click(timeout=1000)
        if checkbox.get_attribute('aria-checked')=='true' or checkbox.evaluate('(el)=>Boolean(el.checked)'):
            raise RuntimeError('自动地点信息未关闭，未提交')
        language=dialog.get_by_role('combobox',name=re.compile('视频语言'))
        if not language.count():
            language=dialog.locator('ytcp-dropdown-trigger').filter(has_text=re.compile('视频语言'))
        if not language.count():
            heading=dialog.get_by_text('视频语言',exact=True)
            language=heading.locator('xpath=ancestor::*[.//ytcp-dropdown-trigger][1]').locator('ytcp-dropdown-trigger')
        self.select_video_language(page,language)
        self.log('[YouTube默认选项] 无付费宣传；AI声明已设置；自动地点信息关闭；视频语言中文（简体）；其余沿用平台默认')

    def select_video_language(self, page, language):
        chinese = re.compile(r'中文\s*[（(]\s*简体\s*[）)]')
        option = page.get_by_role('option',name=re.compile('^'+chinese.pattern+'$'))
        fallback = page.get_by_text(re.compile('^'+chinese.pattern+'$'))
        last_click = [0.0]
        def open_menu():
            dropdown = self.one_visible(language)
            if dropdown is None:
                return None
            if chinese.search(dropdown.inner_text()):
                return 'selected'
            target = self.one_visible(option)
            if target is None and not option.count():
                target = self.one_visible(fallback)
            if target is not None:
                return target
            # Retry actionability failures, but never toggle an already open menu.
            if dropdown.get_attribute('aria-expanded') != 'true' and time.monotonic()-last_click[0] >= 2:
                last_click[0] = time.monotonic()
                dropdown.click(timeout=5000,no_wait_after=True)
            return None
        target = self.wait(page,'打开视频语言菜单：中文（简体）',open_menu,seconds=30)
        if target != 'selected':
            # Resolve the unique visible option again if Studio replaces its DOM.
            self.click(page,option if option.count() else fallback,'选择中文（简体）')
        self.wait(page,'视频语言回读失败，未提交：等待中文（简体）',lambda:
            self.one_visible(language) is not None and chinese.search(self.one_visible(language).inner_text()),seconds=15)

    def select_playlist(self, page, dialog):
        control = dialog.get_by_role('button', name='选择播放列表', exact=True)
        if self.one_visible(control) is None:
            heading = dialog.get_by_text('播放列表', exact=True)
            label = self.one_visible(heading)
            if label is None:
                raise RuntimeError('未找到唯一播放列表标签，请采集该窗口')
            control = label.locator('xpath=ancestor::*[.//ytcp-dropdown-trigger][1]').locator('ytcp-dropdown-trigger')
        current = self.one_visible(control)
        if current is None:
            raise RuntimeError('未找到唯一播放列表下拉框')
        if current.inner_text().strip() == self.account.playlist_name:
            self.log(f'[播放列表] 已选择 {self.account.playlist_name}')
            return
        self.click(page, control, '打开播放列表')
        popup = page.get_by_role('dialog', name='选择播放列表', exact=True)
        self.wait(page, '等待播放列表弹层', lambda: self.one_visible(popup))
        def playlist_item():
            # Real Studio checkboxes have an empty aria-label; the name belongs
            # to the containing label, not the checkbox's accessible name.
            rows = popup.locator('label.ytcp-checkbox-label').filter(
                has_text=re.compile(r'^\s*' + re.escape(self.account.playlist_name) + r'\s*$'))
            row = self.one_visible(rows)
            if row is not None:
                state = self.one_visible(row.get_by_role('checkbox'))
                return (row, state) if state is not None else None
            state = self.one_visible(popup.get_by_role('checkbox', name=self.account.playlist_name, exact=True))
            if state is None:
                return None
            owner = state.locator('xpath=ancestor::ytcp-checkbox-lit[1]')
            return (owner if owner.count() else state, state)
        owner, option = self.wait(page, '查找指定播放列表', playlist_item)
        if option.get_attribute('aria-checked') != 'true':
            self.click(page, owner, '勾选指定播放列表')
        self.wait(page, '回读播放列表勾选', lambda: option.get_attribute('aria-checked') == 'true')
        self.click(page, popup.get_by_role('button', name='完成', exact=True), '确认播放列表')
        self.wait(page, '等待播放列表弹层关闭', lambda: not popup.count() or not popup.is_visible())
        self.wait(page, '回读已保存播放列表',
                  lambda: self.account.playlist_name in [line.strip() for line in current.inner_text().splitlines()])
        self.log(f'[播放列表确认] 已保存 {self.account.playlist_name}')

    def set_thumbnail(self, page, video):
        if self.account.thumbnail_mode == 'platform_default':
            self.log('[缩略图] 使用平台当前视频默认缩略图')
            return
        if self.account.thumbnail_mode != 'video_frame':
            raise PlatformRejected('CONFIG_REQUIRED', '缩略图配置无效，请选择平台默认或视频内置帧')
        editor = self.wait(page, '查找当前视频内置帧入口', lambda: self.one_visible(
            self.dialog(page).locator('ytcp-video-autogenerated-thumbnails-editor')))
        frame_name = re.compile(r'^(?:自动生成的缩略图|Automatically generated thumbnail)\s*\d+$', re.I)

        def saved_frame(name=None):
            preview = self.one_visible(editor.locator('button#preview-button'))
            if preview is None:
                return False
            # The unselected card can already show an automatic image in its
            # select slot. Only its saved preview and matching frame are proof.
            return editor.evaluate('''(el, name) => Array.from(el.querySelectorAll('img')).some(img => {
                const r = img.getBoundingClientRect();
                return r.width > 0 && r.height > 0 && img.complete && img.naturalWidth > 0
                    && getComputedStyle(img).visibility !== 'hidden'
                    && (name ? img.alt === name : /^(自动生成的缩略图|Automatically generated thumbnail)\\s*\\d+$/i.test(img.alt));
            })''', name)

        if saved_frame():
            self.log('[缩略图确认] 当前视频已使用平台内置帧')
            return
        button = editor.locator('button#select-button').filter(
            has_text=re.compile(r'从视频中选择|Choose from video', re.I))
        button = self.wait(page, '查找从视频中选择入口', lambda: self.one_visible(button))
        # Studio ignores a tap targeting YT-ICON; the tall card's centre is an
        # icon. Click its caption, without activating any upload-file control.
        caption = button.locator('#select-text')
        self.click(page, caption, '从当前视频选择内置帧')
        def frame_picker():
            # The live Studio popup is a tp-yt-paper-dialog with this accessible
            # name. Older captures wrapped it in the autogenerated host tag.
            for locator in (page.get_by_role('dialog', name=re.compile(
                    r'从视频中选择一帧画面|Choose (?:a )?(?:frame|thumbnail) from (?:the )?video', re.I)),
                    page.locator('ytcp-video-autogenerated-thumbnails-dialog')):
                found = self.one_visible(locator)
                if found is not None:
                    return found
        picker = self.wait(page, '等待视频内置帧选择窗口', frame_picker)

        frame_progress=[None]
        def available_frame():
            options = picker.get_by_role('option', name=frame_name)
            state=options.evaluate_all('''els=>({total:els.length, enabled:els.filter(e=>!e.disabled&&e.getAttribute('aria-disabled')!=='true').length,
                loaded:els.filter(e=>Array.from(e.querySelectorAll('img')).some(i=>i.complete&&i.naturalWidth>0)).length})''')
            if state!=frame_progress[0]:
                self.log(f'[视频帧状态] 候选={state["total"]}，可操作={state["enabled"]}，图片已加载={state["loaded"]}；等待真实帧就绪，不使用占位图片')
                frame_progress[0]=state
            for index in range(options.count()):
                option = options.nth(index)
                if option.is_visible() and option.is_enabled() and option.evaluate('''el =>
                    Array.from(el.querySelectorAll('img')).some(img =>
                        img.complete && img.naturalWidth > 0 && img.getBoundingClientRect().width > 0
                        && getComputedStyle(img).visibility !== 'hidden')'''):
                    name = option.get_attribute('aria-label')
                    return self.one_visible(picker.get_by_role('option', name=name, exact=True))
            return None

        frame = self.wait(page, '等待平台生成可选视频帧', available_frame, seconds=1800)
        name = frame.get_attribute('aria-label')
        done=picker.get_by_role('button',name=re.compile(r'^(完成|Done)$',re.I))
        if (frame.get_attribute('aria-selected')=='true' and done.count()==1
                and done.is_visible() and not done.is_enabled()):
            # An unchanged native frame disables Done. Cancel, reopen and verify
            # that Studio persists the same selected frame; never force Done or
            # confuse the select-card placeholder with a newly saved thumbnail.
            cancel=lambda: self.click(page,picker.get_by_role('button',name=re.compile(r'^(取消|Cancel)$',re.I)),
                                      '关闭未变更的视频帧选择')
            cancel()
            self.wait(page,'确认视频帧窗口已关闭',lambda:not picker.is_visible())
            self.click(page,caption,'重新核验平台已有视频帧')
            picker=self.wait(page,'等待视频内置帧选择窗口',frame_picker)
            current=picker.get_by_role('option',name=name,exact=True)
            def persisted():
                item=self.one_visible(current)
                if item is None or item.get_attribute('aria-selected')!='true':return False
                return item.evaluate('''el=>Array.from(el.querySelectorAll('img')).some(i=>
                    i.complete&&i.naturalWidth>0&&i.getBoundingClientRect().width>0
                    &&getComputedStyle(i).visibility!=='hidden')''')
            self.wait(page,'核验平台保留同一个已选视频帧',persisted,seconds=60)
            done=picker.get_by_role('button',name=re.compile(r'^(完成|Done)$',re.I))
            if done.count()!=1 or not done.is_visible() or done.is_enabled():
                raise RuntimeError('现有视频帧仍有未保存变更，未将其认定为已保存')
            cancel()
            self.wait(page,'确认视频帧窗口已关闭',lambda:not picker.is_visible())
            self.log('[缩略图确认] 已重新打开并核验平台保留原视频帧：'+name+'；沿用已有选择，无需重复保存')
            return
        if frame.get_attribute('aria-selected') != 'true':
            self.click(page, frame, '选择'+name)
        self.wait(page, '确认视频帧已选中', lambda: frame.get_attribute('aria-selected') == 'true')
        self.click(page, picker.get_by_role('button', name=re.compile(r'^(完成|Done)$', re.I)),
                   '保存视频内置帧')
        self.wait(page, '确认视频帧窗口已关闭', lambda: not picker.is_visible())
        self.wait(page, '确认视频内置帧已保存', lambda: saved_frame(name))
        self.log('[缩略图确认] 已保存当前视频内置帧：'+name)

    def read_upload_state(self, page):
        return self.dialog(page).evaluate(r'''el => {
            const visible = e=> {const r=e.getBoundingClientRect();return r.width>0&&r.height>0&&getComputedStyle(e).visibility!=='hidden'};
            const leaves = Array.from(el.querySelectorAll('*')).filter(e=>!e.children.length && visible(e)
                && !e.closest('[contenteditable],textarea,input')).map(e=>(e.innerText||'').trim());
            const progress = Array.from(el.querySelectorAll('[role=progressbar],progress')).filter(visible);
            const busy = leaves.some(t=>/^(正在上传|上传中|正在处理|处理中|Uploading|Processing)/i.test(t))
                || leaves.some(t=> {const m=t.match(/^(\d+(?:\.\d+)?)%$/);return m && Number(m[1])<100})
                || Array.from(el.querySelectorAll('[aria-busy=true]')).some(visible)
                || progress.some(e=> {const n=Number(e.getAttribute('aria-valuenow')??e.value??-1);
                    return n>=0&&n<Number(e.getAttribute('aria-valuemax')||e.max||100)});
            const complete = leaves.some(t=>/^(检查完毕|检查完成|处理完毕|上传完成|Checks complete|Processing complete)/i.test(t));
            const links = Array.from(el.querySelectorAll('a[href]')).map(a=>a.href);
            const video = Array.from(el.querySelectorAll('video')).some(v=>v.readyState>=1);
            return {busy, complete, links, video};
        }''')

    def wait_upload(self,page,seconds=1800):
        self._wait_for_upload_ready(page,seconds)
        self.log(f'[上传完成] 视频ID {self.video_id}；尚未确认发布')

    def prepare_visibility(self,page):
        for name in ('视频元素','检查','公开范围'):
            self.click(page,self.dialog(page).get_by_role('tab',name=name,exact=True),f'进入{name}')
        self.set_visibility(page)

    def submit_control(self,page):
        button=self.dialog(page).get_by_role('button',name='发布' if self.account.visibility=='public' else '保存',exact=True)
        return self.wait(page,'校验最终提交按钮',lambda:self.one_visible(button))


    def set_visibility(self, page):
        labels = {'public': '公开', 'unlisted': '不公开列出', 'private': '私享'}
        label = labels[self.account.visibility]
        self.click(page, self.dialog(page).get_by_role('radio', name=label, exact=True), f'设置公开范围：{label}')
        selected = self.one_visible(self.dialog(page).get_by_role('radio', name=label, exact=True))
        if selected.get_attribute('aria-checked') != 'true':
            raise RuntimeError('公开范围回读失败，未执行最终提交')

    def verify_persisted_result(self, context, page, title, seconds=600):
        # A created link or private auto-save is not proof of the requested final visibility.
        verify_page = context.new_page()
        labels = {'public': '公开', 'unlisted': '不公开列出', 'private': '私享'}
        url = f'https://studio.youtube.com/channel/{self.account.channel_id}/videos'
        category = 0
        polls = 0
        def confirmed():
            nonlocal category,polls
            self.check_rejection(page)
            # Only the final, current public submission's completion dialog is evidence.
            # An upload/draft link alone must never confirm visibility.
            if self.submitted and getattr(self,'_publish_armed',False) and self.account.visibility=='public':
                dialogs=page.locator('ytcp-video-share-dialog, ytcp-video-publish-dialog, [role="dialog"]')
                for index in range(dialogs.count()):
                    dialog=dialogs.nth(index)
                    if not dialog.is_visible():
                        continue
                    text=dialog.inner_text()
                    if title not in text or not re.search(r'视频发布时间|视频已发布|Video published',text,re.I):
                        continue
                    links=dialog.locator('a[href]').evaluate_all('(els)=>els.map(el=>el.href)')
                    if any(youtube_video_id(link)==self.video_id for link in links):
                        # Studio navigates to /video/<id>/edit after publishing.
                        # That URL cannot supply a channel ID: verify it on a separate
                        # channel page while preserving the current submission proof.
                        parsed = urlsplit(page.url)
                        if parsed.hostname != 'studio.youtube.com':
                            continue
                        if parsed.path.startswith('/channel/'):
                            self.verify_channel(page)
                        elif parsed.path != f'/video/{self.video_id}/edit':
                            continue
                        try:
                            verify_page.goto(url, wait_until='domcontentloaded', timeout=10000)
                        except PlaywrightTimeoutError:
                            self.log('[频道核对等待] 完成弹窗已匹配，独立频道页面加载超时，继续等待核验')
                            return None
                        self.verify_channel(verify_page)
                        self.log('[发布确认] 本次公开发布完成弹窗确认目标视频ID与标题')
                        return True
            try:
                if polls%2==0:
                    verify_page.goto(url, wait_until='domcontentloaded', timeout=10000)
                    self.verify_channel(verify_page)
                    shorts = self.is_short if category==0 else not self.is_short
                    category=1-category
                    if shorts:
                        tabs=verify_page.get_by_role('tab',name='Shorts',exact=True)
                        if not tabs.count():
                            tabs=verify_page.get_by_text('Shorts',exact=True)
                        visible=self.one_visible(tabs)
                        if visible is not None:
                            visible.click(timeout=1000)
                            self.log('[结果页面] 已切换Shorts，按视频ID核对，不以草稿或标题相同判断成功')
                polls+=1
                # Give the existing page a second poll before refreshing it.
                self._pause(verify_page,1000)
                rows = verify_page.locator('ytcp-video-row').filter(
                    has=verify_page.locator(f'a[href*="/video/{self.video_id}/"]'))
                if rows.count() == 1:
                    row = rows.first
                    visibility = row.get_by_text(labels[self.account.visibility], exact=True)
                    if title in row.inner_text() and visibility.count() == 1 and visibility.is_visible():
                        self.log('[结果核对] 内容管理页确认当前视频ID、标题及目标公开范围')
                        return True
            except PlaywrightTimeoutError:
                pass
            self.log('[发布核对] 尚未在内容管理页确认目标状态，保留浏览器与原文件')
            return None
        result = False
        try:
            result = bool(self._wait_for_confirmation(page,confirmed,seconds,poll_ms=10000))
            return result
        finally:
            if not result:
                self._capture_failure_page(verify_page, '内容管理页未确认目标视频状态', role='verification')
            try:
                verify_page.close()
            except Exception:
                pass

    def upload_single_video(self, video):
        context, page = None, None
        self.last_outcome, self.submitted, self.video_id = 'FAILED', False, ''
        self.is_short=False
        self._video_selected,self._publish_armed,self._upload_activity_seen=False,False,False
        self._checkpoint_video_id=''
        try:
            self.title=resolve_title(self.account,video.name)[:100]
        except ValueError as error:
            self.last_outcome='CONFIG_REQUIRED'
            return False,str(error),None
        try:
            self.stop_check()
            self.log('[YouTube流程 20261018.6] '+('恢复既有草稿' if self.resume_info else '上传新文件')+
                '；缩略图模式='+self.account.thumbnail_mode+'；目标公开范围='+self.account.visibility)
            if self.account.made_for_kids is None:
                raise PlatformRejected('CONFIG_REQUIRED', '未配置是否面向儿童')
            if self.account.visibility not in ('public', 'private', 'unlisted'):
                raise PlatformRejected('CONFIG_REQUIRED', '公开范围配置无效')
            self.profile.mkdir(parents=True, exist_ok=True)
            with sync_playwright() as p:
                try:
                    context = launch_youtube_context(p, self.profile, headless=self.headless,
                        viewport={'width': 1440, 'height': 900}, timeout=10000)
                    self.log(youtube_runtime_summary(context, self.profile))
                    context.set_default_timeout(1000)
                    page = context.pages[0] if context.pages else context.new_page()
                    self.attach_network_diagnostics(page)
                    page.goto('https://studio.youtube.com/', wait_until='domcontentloaded', timeout=10000)
                    self.verify_channel(page)
                    self.log('[登录状态] 已核对当前Google登录及绑定频道；尚未上传文件')
                    if isinstance(self.resume_info,dict) and self.resume_info.get('video_id'):
                        self.open_saved_draft(page,self.resume_info)
                    else:
                        file_input = self.open_upload(page)
                        self.stop_check()
                        self.before_upload()
                        file_input.set_input_files(str(video.resolve()))
                        self._video_selected=True
                        self.log('[正在上传] '+video.name+'；已选择本地文件，尚未发布')
                    submit=self.prepare_publish_form(page,
                        details=('填写视频详细信息',lambda:self.fill_details(page,video)),
                        cover=('设置视频缩略图',lambda:self.set_thumbnail(page,video)),
                        collection=('配置播放列表',lambda:self.select_playlist(page,self.dialog(page)) if self.account.playlist_name else None),
                        upload=('等待视频上传、处理与检查完成',lambda:self.wait_upload(page)),
                        prepare_submit=('配置视频元素、检查及公开范围',lambda:self.prepare_visibility(page)),
                        submit=('校验最终提交按钮',lambda:self.submit_control(page)),
                        collection_before_cover=True, uploaded_before_options=True)
                    title=self.title
                    self.stop_check()
                    self.before_submit(self.result_hint())
                    self.stop_check()
                    self.submitted,self._publish_armed = True,True  # A timeout after this boundary must not be blindly retried.
                    submit.click(timeout=1000)
                    if not self.verify_persisted_result(context, page, title):
                        raise RuntimeError('平台最终公开范围尚未确认')
                    self.last_outcome = 'SUCCESS'
                    return True, '已确认目标公开范围，等待统一归档', {
                        'video_id': self.video_id, 'url': f'https://www.youtube.com/watch?v={self.video_id}',
                        'visibility': self.account.visibility, 'channel_id': self.account.channel_id, 'title': title}
                except Exception as failure:
                    self._capture_failure_page(page, failure)
                    if page and not page.is_closed():
                        try:
                            probe = ChannelDOMProbe(self.account, self.report_root)
                            report, _ = probe.capture(page, '异常提示')
                            self.log(f'[诊断] 阶段{self.stage}：{report}')
                        except Exception as error:
                            self.log(f'[诊断保存失败] {error}')
                    raise
                finally:
                    if context:
                        try:
                            self._close_publish_context(context,page,self.submitted)
                        except Exception:
                            pass
        except UploadStopped:
            self.last_outcome = 'FAILED' if self._video_selected else 'CANCELLED'
            return False, '用户停止；未确认成功按失败处理' if self.submitted else '用户停止；未提交发布', self.result_hint()
        except PlatformRejected as error:
            self.last_outcome = error.status
            return False, str(error), self.result_hint()
        except Exception as error:
            self.last_outcome = 'FAILED'
            message=str(error)
            if getattr(self,'network_errors',[]):message+='；可见网络异常：'+'；'.join(self.network_errors[-5:])
            return False, message, self.result_hint()

    def result_hint(self):
        return {'video_id': self.video_id, 'channel_id': self.account.channel_id,
                'visibility': self.account.visibility, 'title': self.title,
                'is_short':self.is_short,'draft':not self.submitted,'submitted':self.submitted} if self.video_id else None

    def reconcile(self, info):
        """Read-only recheck of a previous submission; never clicks upload or publish."""
        if (not info or info.get('channel_id') != self.account.channel_id or
            info.get('visibility') != self.account.visibility or not info.get('title')):
            return False
        self.video_id = info['video_id']
        self.is_short=bool(info.get('is_short'))
        confirmed = False
        with sync_playwright() as p:
            context = launch_youtube_context(p, self.profile, headless=self.headless,
                viewport={'width': 1440, 'height': 900}, timeout=10000)
            self.log(youtube_runtime_summary(context, self.profile))
            try:
                page = context.pages[0] if context.pages else context.new_page()
                context.set_default_timeout(1000)
                page.goto('https://studio.youtube.com/', wait_until='domcontentloaded', timeout=10000)
                self.verify_channel(page)
                confirmed = self.verify_persisted_result(context, page, info['title'], seconds=60)
                return confirmed
            finally:
                if not confirmed:
                    self._capture_failure_page(locals().get('page'), '历史投稿结果未确认')
                context.close()
