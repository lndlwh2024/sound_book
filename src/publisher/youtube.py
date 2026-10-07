"""YouTube Studio publisher. Upload, draft and persisted visibility are separate states."""
import re
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
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


class YouTubeUploader:
    def __init__(self, account, profile, report_root, stop_requested=lambda: False,
                 progress_callback=lambda message: None, headless=False):
        self.account, self.profile, self.report_root = account, Path(profile), Path(report_root)
        self.stop_requested, self.log, self.headless = stop_requested, progress_callback, headless
        self.last_outcome = 'FAILED'
        self.submitted = False
        self.video_id = ''
        self.title = ''
        self.stage = '启动'
        self.before_submit = lambda info: None

    def stop_check(self):
        if self.stop_requested():
            raise UploadStopped()

    def pause(self, page, milliseconds=300):
        for step in range(0, milliseconds, 300):
            self.stop_check()
            page.wait_for_timeout(min(300, milliseconds - step))
        self.stop_check()
        self.check_rejection(page)

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

    def wait(self, page, label, condition, seconds=60):
        self.stage = label
        self.log(label)
        deadline, last_log = time.monotonic() + seconds, 0
        while time.monotonic() < deadline:
            self.stop_check()
            self.check_rejection(page)
            try:
                result = condition()
                if result:
                    return result
            except PlaywrightTimeoutError:
                pass
            if time.monotonic() - last_log > 30:
                self.log(f'[等待] {label}；浏览器保持打开')
                last_log = time.monotonic()
            self.pause(page)
        raise RuntimeError(f'{label}超时')

    @staticmethod
    def one_visible(locator):
        visible = [locator.nth(i) for i in range(locator.count()) if locator.nth(i).is_visible()]
        if len(visible) > 1:
            raise RuntimeError('存在多个匹配控件，停止以避免操作错误')
        return visible[0] if visible and visible[0].is_enabled() else None

    def click(self, page, locator, label):
        button = self.wait(page, label, lambda: self.one_visible(locator))
        self.stop_check()
        button.click(timeout=1000)

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
        if self.account.altered_content is not None:
            control = dialog.get_by_role('radio', name='是，使用了 AI' if self.account.altered_content else '否，未使用 AI', exact=True)
            self.click(page, control, '设置AI内容声明')
        if self.account.playlist_name:
            self.select_playlist(page, dialog)
        self.title = title
        return title

    def select_playlist(self, page, dialog):
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
        options = page.get_by_role('checkbox', name=self.account.playlist_name, exact=True)
        option = self.wait(page, '查找指定播放列表', lambda: self.one_visible(options))
        if option.get_attribute('aria-checked') != 'true':
            option.click(timeout=1000)
        if option.get_attribute('aria-checked') != 'true':
            raise RuntimeError('播放列表勾选回读失败')
        self.click(page, page.get_by_role('button', name='完成', exact=True), '确认播放列表')

    def set_thumbnail(self, page, video):
        if self.account.thumbnail_mode == 'platform_default':
            self.log('[缩略图] 使用平台当前视频默认缩略图')
            return
        inputs = self.dialog(page).locator('input[type=file][accept*="image"]')
        if inputs.count() != 1:
            raise PlatformRejected('PERMANENT_REJECT', '当前账号/视频未提供唯一自定义缩略图入口，请选择平台默认或采集缩略图窗口')
        with tempfile.TemporaryDirectory(prefix='shusheng_thumbnail_') as directory:
            image = Path(directory) / 'frame.jpg'
            self.stop_check()
            subprocess.run(['ffmpeg', '-y', '-ss', '0', '-i', str(video), '-frames:v', '1', str(image)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True, timeout=15,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self.stop_check()
            inputs.set_input_files(str(image))
            self.log('[缩略图] 已提交当前视频首帧图片')

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

    def wait_upload(self, page, seconds=1800):
        def ready():
            state = self.read_upload_state(page)
            for link in state['links']:
                candidate = youtube_video_id(link)
                if candidate:
                    self.video_id = candidate
            return state['complete'] and not state['busy'] and bool(self.video_id)
        self.wait(page, '等待视频上传、处理与检查完成', ready, seconds)
        self.log(f'[上传完成] 视频ID {self.video_id}；尚未确认发布')

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
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.stop_check()
            self.check_rejection(page)
            try:
                verify_page.goto(url, wait_until='domcontentloaded', timeout=10000)
                self.verify_channel(verify_page)
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
            self.pause(page, 10000)
        return False

    def upload_single_video(self, video):
        context, page = None, None
        self.last_outcome, self.submitted, self.video_id = 'FAILED', False, ''
        try:
            self.stop_check()
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
                    file_input.set_input_files(str(video.resolve()))
                    title = self.fill_details(page, video)
                    self.set_thumbnail(page, video)
                    self.wait_upload(page)
                    for name in ('视频元素', '检查', '公开范围'):
                        self.click(page, self.dialog(page).get_by_role('tab', name=name, exact=True), f'进入{name}')
                    self.set_visibility(page)
                    button = self.dialog(page).get_by_role('button', name='发布' if self.account.visibility == 'public' else '保存', exact=True)
                    submit = self.wait(page, '校验最终提交按钮', lambda: self.one_visible(button))
                    self.stop_check()
                    self.before_submit(self.result_hint())
                    self.stop_check()
                    self.submitted = True  # A timeout after this boundary must not be blindly retried.
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
                            context.close()
                        except Exception:
                            pass
        except UploadStopped:
            self.last_outcome = 'UNCONFIRMED' if self.submitted else 'CANCELLED'
            return False, '用户停止；已提交待核对' if self.submitted else '用户停止；未提交发布', self.result_hint()
        except PlatformRejected as error:
            self.last_outcome = error.status
            return False, str(error), self.result_hint()
        except Exception as error:
            self.last_outcome = 'UNCONFIRMED' if self.submitted else 'FAILED'
            return False, str(error), self.result_hint()

    def result_hint(self):
        return {'video_id': self.video_id, 'channel_id': self.account.channel_id,
                'visibility': self.account.visibility, 'title': self.title} if self.video_id else None

    def reconcile(self, info):
        """Read-only recheck of a previous submission; never clicks upload or publish."""
        if (not info or info.get('channel_id') != self.account.channel_id or
            info.get('visibility') != self.account.visibility or not info.get('title')):
            return False
        self.video_id = info['video_id']
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(str(self.profile), headless=self.headless,
                viewport={'width': 1440, 'height': 900}, timeout=10000)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                context.set_default_timeout(1000)
                page.goto('https://studio.youtube.com/', wait_until='domcontentloaded', timeout=10000)
                self.verify_channel(page)
                return self.verify_persisted_result(context, page, info['title'], seconds=30)
            finally:
                context.close()
