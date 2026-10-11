"""YouTube Studio publisher. Upload, draft and persisted visibility are separate states."""
import re
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from .browser_workflow import BrowserPublishWorkflow
from .channel_probe import ChannelDOMProbe
from .douyin.uploader import resolve_publish_series


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
                raise PlatformRejected('AUTH_REQUIRED', 'Google登录已失效，请先登录')
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
            candidates = page.locator('input[type=file]')
            allowed = [candidates.nth(i) for i in range(candidates.count())
                       if not re.search(r'image|png|jpg|jpeg', candidates.nth(i).get_attribute('accept') or '', re.I)]
            if len(allowed) != 1:
                return None
            return allowed[0]
        return self.wait(page, '等待视频文件输入框', file_input)

    def dialog(self, page):
        return page.locator('ytcp-uploads-dialog [role=dialog]:visible').first

    def fill_details(self, page, video):
        title = resolve_publish_series(video.name) if self.account.title_mode == 'series' else video.stem
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
            button=self.wait(page,'等待YouTube高级设置入口',advanced_control)
            self.click(page,button,'展开YouTube详细选项')
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
        dropdown=self.wait(page,'设置视频语言：中文（简体）',lambda:self.one_visible(language))
        if not re.search(r'中文\s*[（(]\s*简体\s*[）)]',dropdown.inner_text()):
            dropdown.click(timeout=1000)
            option=page.get_by_role('option',name=re.compile(r'^中文\s*[（(]\s*简体\s*[）)]$'))
            if not option.count():
                option=page.get_by_text(re.compile(r'^中文\s*[（(]\s*简体\s*[）)]$'))
            self.click(page,option,'选择中文（简体）')
        if not re.search(r'中文\s*[（(]\s*简体\s*[）)]',dropdown.inner_text()):
            raise RuntimeError('视频语言回读失败，未提交')
        self.log('[YouTube默认选项] 无付费宣传；AI声明已设置；自动地点信息关闭；视频语言中文（简体）；其余沿用平台默认')

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
        picker = self.wait(page, '等待视频内置帧选择窗口', lambda: self.one_visible(
            page.locator('ytcp-video-autogenerated-thumbnails-dialog')))

        def available_frame():
            options = picker.get_by_role('option', name=frame_name)
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
                        self.verify_channel(page)
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
                        if tabs.count()==1 and tabs.is_visible():
                            tabs.click(timeout=1000)
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
        return bool(self._wait_for_confirmation(page,confirmed,seconds,poll_ms=10000))

    def upload_single_video(self, video):
        context, page = None, None
        self.last_outcome, self.submitted, self.video_id = 'FAILED', False, ''
        self.is_short=False
        self._video_selected,self._publish_armed,self._upload_activity_seen=False,False,False
        try:
            self.stop_check()
            self.log('[YouTube流程] 从视频中选择内置帧；保存并回读封面（20261011-6）')
            if self.account.made_for_kids is None:
                raise PlatformRejected('CONFIG_REQUIRED', '未配置是否面向儿童')
            if self.account.visibility not in ('public', 'private', 'unlisted'):
                raise PlatformRejected('CONFIG_REQUIRED', '公开范围配置无效')
            self.profile.mkdir(parents=True, exist_ok=True)
            with sync_playwright() as p:
                try:
                    context = p.chromium.launch_persistent_context(str(self.profile), headless=self.headless,
                        viewport={'width': 1440, 'height': 900}, timeout=10000)
                    context.set_default_timeout(1000)
                    page = context.pages[0] if context.pages else context.new_page()
                    page.goto('https://studio.youtube.com/', wait_until='domcontentloaded', timeout=10000)
                    self.verify_channel(page)
                    file_input = self.open_upload(page)
                    self.stop_check()
                    self.before_upload()
                    file_input.set_input_files(str(video.resolve()))
                    self._video_selected=True
                    submit=self.prepare_publish_form(page,
                        details=('填写视频详细信息',lambda:self.fill_details(page,video)),
                        cover=('设置视频缩略图',lambda:self.set_thumbnail(page,video)),
                        collection=('配置播放列表',lambda:self.select_playlist(page,self.dialog(page)) if self.account.playlist_name else None),
                        upload=('等待视频上传、处理与检查完成',lambda:self.wait_upload(page)),
                        prepare_submit=('配置视频元素、检查及公开范围',lambda:self.prepare_visibility(page)),
                        submit=('校验最终提交按钮',lambda:self.submit_control(page)),
                        collection_before_cover=True)
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
                except Exception:
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
            return False, str(error), self.result_hint()

    def result_hint(self):
        return {'video_id': self.video_id, 'channel_id': self.account.channel_id,
                'visibility': self.account.visibility, 'title': self.title,
                'is_short':self.is_short} if self.video_id else None

    def reconcile(self, info):
        """Read-only recheck of a previous submission; never clicks upload or publish."""
        if (not info or info.get('channel_id') != self.account.channel_id or
            info.get('visibility') != self.account.visibility or not info.get('title')):
            return False
        self.video_id = info['video_id']
        self.is_short=bool(info.get('is_short'))
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(str(self.profile), headless=self.headless,
                viewport={'width': 1440, 'height': 900}, timeout=10000)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                context.set_default_timeout(1000)
                page.goto('https://studio.youtube.com/', wait_until='domcontentloaded', timeout=10000)
                self.verify_channel(page)
                return self.verify_persisted_result(context, page, info['title'], seconds=60)
            finally:
                context.close()
