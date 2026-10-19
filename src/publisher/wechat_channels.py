"""WeChat Channels adapter for the shared account/upload/confirmation workflow.

Only browser controls submit posts. Response listeners observe acknowledgements;
they never call private endpoints or replay authenticated requests.
"""
import re
from urllib.parse import urlsplit

from .channel_probe import CREATOR_NOTICES_JS, safe_url
from .creator_studio import CreatorStudioUploader, rejection
from .youtube import PlatformRejected


PUBLISH_PATH = '/cgi-bin/mmfinderassistant-bin/post/post_create'
SUCCESS_TEXT = re.compile(r'^发表成功[！!。\s]*(?:.*审核.*)?$')


def publish_endpoint(url):
    try:
        address = urlsplit(url)
        return (address.scheme == 'https' and address.hostname == 'channels.weixin.qq.com'
                and address.port in (None, 443) and address.path == PUBLISH_PATH)
    except (ValueError, TypeError):
        return False


def acknowledgement(body, http_status):
    """An HTTP 200, draft ID or preview response alone never confirms a post."""
    if not isinstance(body, dict):
        return None
    code = body.get('errCode')
    if isinstance(code, bool) or not isinstance(code, (int, str)):
        return None
    if not re.fullmatch(r'-?\d+', str(code)):
        return None
    data = body.get('data')
    data = data if isinstance(data, dict) else {}
    inner = data.get('baseResp')
    if isinstance(inner, dict):
        inner_code = inner.get('errcode')
        if isinstance(inner_code, bool) or str(inner_code) != '0':
            return None
    if not 200 <= http_status < 300 or str(code) != '0' or body.get('success') is False:
        return None
    ids = {str(data[key]) for key in ('objectId', 'object_id', 'feedId', 'feed_id')
           if isinstance(data.get(key), (str, int)) and not isinstance(data[key], bool)
           and str(data[key])}
    if len(ids) > 1:
        return None
    result = {'kind': 'wechat_publish_response', 'endpoint': 'https://channels.weixin.qq.com'+PUBLISH_PATH,
              'accepted': True, 'business_code': 0}
    if ids:
        result['post_id'] = ids.pop()
    return result


class WeChatChannelsUploader(CreatorStudioUploader):
    title_selector = '.post-desc-box .input-editor[contenteditable], .input-editor[contenteditable=true]'

    def check_rejection(self, page):
        address = urlsplit(page.url)
        if address.hostname != self.channel.host or not address.path.startswith('/platform/'):
            raise PlatformRejected('AUTH_REQUIRED', '微信视频号需要扫码登录；未确认登录，不上传文件')
        super().check_rejection(page)
        if self.confirmation:
            return
        for item in page.evaluate(CREATOR_NOTICES_JS):
            text = item['text']
            state = rejection(text.replace('发表', '发布'))
            if re.search(r'请.{0,12}(填写|输入|添加).{0,8}(描述|短标题)|请选择视频标注', text):
                state = 'CONFIG_REQUIRED'
            if state and (item['notice'] or re.match(r'^(发表失败|网络出错，请重新上传)', text)):
                if self.submitted and re.match(r'^发表失败', text):
                    self.business_rejected = True
                raise PlatformRejected(state, text)

    def publish_control(self, page, *, inspect_only=False):
        candidates = page.get_by_role('button', name='发表', exact=True)
        if not any(candidates.nth(i).is_visible() for i in range(candidates.count())):
            candidates = page.get_by_text('发表', exact=True)
        owners = []
        for index in range(candidates.count()):
            label = candidates.nth(index)
            if label.is_visible():
                owners.append(self.control_details(self.click_owner(label)))
        self.publish_details = {'matched': len(owners), 'owners': owners}
        if inspect_only:
            return len(owners) == 1 and self.control_usable(owners[0])
        button = self.clickable(candidates)
        return button if button and self.control_usable(self.control_details(button)) else None

    def fill_details(self, page, video):
        self.title = self.account.wechat_short_title.strip() or video.stem
        editor = self.wait(page, '等待微信视频号描述编辑区',
                           lambda: self.one_visible(page.locator(self.title_selector)), self.upload_seconds)
        # This platform deliberately has no description/title-rule setting.
        # Clear a restored form value as well, instead of posting stale content.
        self.fill_and_verify(page, editor, '', '视频描述')
        short = self.account.wechat_short_title.strip()
        if short:
            control = self.wait(page, '等待微信视频号短标题', lambda: self.one_visible(
                page.locator('input[placeholder*="短标题"]')), self.control_seconds)
            self.fill_and_verify(page, control, short, '短标题')
        self.log('[内容填写] 视频描述留空；短标题='+('使用已配置内容' if short else '未指定，保留平台默认'))

    def fill_and_verify(self, page, control, value, label):
        limit = control.get_attribute('maxlength')
        if limit and limit.isdigit() and len(value) > int(limit):
            raise PlatformRejected('CONFIG_REQUIRED', f'{label}超过当前页面的 {limit} 字限制；未截断内容、未发表')
        self.stop_check()
        control.fill(value, timeout=1500)
        self.wait(page, '核对'+label, lambda: control.evaluate(
            "e=>e.matches('input,textarea')?e.value:(e.innerText||'')").strip() == value.strip(), 5)

    def read_upload_state(self, page):
        state = super().read_upload_state(page)
        extra = page.evaluate(r'''() => {
            const visible=e=>!!(e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden');
            const busy=[...document.querySelectorAll('.post-upload-wrap .upload-progress,'
                +'.post-upload-wrap .uploading-wrap,.post-video-cover-wrap .loading-wrap')].some(visible);
            const labels=[...document.querySelectorAll('.post-upload-wrap .msg-tip,'
                +'.post-upload-wrap .video-cannot-play-text,.post-video-cover-wrap .gen-text')]
                .filter(visible).map(e=>(e.innerText||'').trim());
            return {busy:busy||labels.some(t=>/正在处理文件|生成中/.test(t)),
                complete:labels.some(t=>/^视频已上传/.test(t)),
                failed:labels.filter(t=>/网络出错|上传失败|处理失败/.test(t))};
        }''')
        if extra['failed']:
            raise PlatformRejected('FAILED', '；'.join(extra['failed']))
        state['busy'] = state['busy'] or extra['busy']
        state['complete'] = state['complete'] or extra['complete']
        if extra['busy']:
            state['pending'].append('微信视频号上传、处理或封面生成中')
        return state

    def form_row(self, page, label):
        rows = page.locator('.form-item').filter(has=page.get_by_text(label, exact=True))
        row = self.one_visible(rows, enabled=False)
        if row is None:
            raise PlatformRejected('CONFIG_REQUIRED', f'未找到唯一“{label}”设置区域；未发表，请采集当前页面')
        return row

    def select_named_option(self, page, label, value, display_selector, option_selector):
        row = self.form_row(page, label)
        def selected():
            display = self.one_visible(row.locator(display_selector), enabled=False)
            return display and display.inner_text().strip() == value
        if selected():
            self.log(f'[参数确认] {label}：{value}；已选中')
            return
        control = self.one_visible(row.locator(display_selector))
        if control is None:
            raise PlatformRejected('CONFIG_REQUIRED', f'“{label}”选择入口不可用；未发表')
        self.stop_check()
        control.click(timeout=1500)
        candidates = row.locator(option_selector).get_by_text(value, exact=True)
        try:
            option = self.wait(page, f'查找{label}：{value}', lambda: self.one_visible(candidates), 10)
            self.stop_check()
            option.click(timeout=1500)
            self.wait(page, f'回读{label}：{value}', selected, 5)
        except RuntimeError as error:
            raise PlatformRejected('CONFIG_REQUIRED', f'“{label}”选项“{value}”未找到或回读失败；未发表') from error
        self.log(f'[参数确认] {label}：{value}；已回读')

    def select_collection(self, page):
        self.select_named_option(page, '位置', '不显示位置',
                                 '.post-position-wrap .position-display-wrap',
                                 '.post-position-wrap .location-filter-wrap')
        collection = self.account.collection_name.strip()
        if collection:
            self.select_named_option(page, '添加到合集', collection,
                                     '.post-album-display', '.post-album-wrap .filter-wrap')
        else:
            self.log('[合集] 未指定合集，保留本次发表页面默认设置，不创建新合集')
        self.log('[默认参数] 链接、活动、定时发表、视频标注及其他选项使用平台默认；不自动声明原创或接受分成协议')
        timed = page.locator('.form-item').filter(has=page.get_by_text('定时发表', exact=True))
        if timed.count() and timed.locator('input[type=radio]:checked').count():
            checked = timed.locator('input[type=radio]:checked')
            if checked.count() != 1 or checked.first.input_value() != '0':
                raise PlatformRejected('CONFIG_REQUIRED', '当前页面选中了定时发表；本版本只执行即时发表，未提交')

    def success_notice(self, page):
        # Only platform notices count; a description/title containing "发表成功"
        # or a management-list row is never evidence for the current file.
        notices = page.locator('[role=alert], [class*=toast], .weui-desktop-msg')
        for index in range(notices.count()):
            item = notices.nth(index)
            if item.is_visible() and SUCCESS_TEXT.fullmatch(item.inner_text().strip()):
                return item
        return None

    def attach_responses(self, page):
        requests = set()
        def started(request):
            if self.submitted and request.method == 'POST' and publish_endpoint(request.url):
                requests.add(request)
        def received(response):
            if not self.submitted or response.request not in requests:
                return
            requests.discard(response.request)
            if not publish_endpoint(response.url):
                return
            try:
                body = response.json()
            except Exception:
                return
            confirmation = acknowledgement(body, response.status)
            message = str(body.get('errMsg') or body.get('message') or '')[:250] if isinstance(body, dict) else ''
            code = body.get('errCode') if isinstance(body, dict) else None
            self.log(f'[微信视频号发表回执] {safe_url(response.url)}；HTTP {response.status}；业务码 {code}；{message}')
            if confirmation:
                self.confirmation = confirmation
                self.before_submit(self.result_hint())
                self.log('[提交确认] 微信视频号已接收本次正式发表；平台后续审核由平台处理')
            elif response.status == 429:
                self.account_block = {'status': 'PLATFORM_BUSY', 'message': message or '平台请求过于频繁'}
                self.platform_error = ('PLATFORM_BUSY', self.account_block['message'])
            elif (200 <= response.status < 300 and isinstance(code, (str, int))
                  and not isinstance(code, bool) and re.fullmatch(r'-?\d+', str(code)) and str(code) != '0'):
                self.business_rejected = True
                self.platform_error = (rejection(message.replace('发表', '发布')) or 'FAILED', message or f'发表被拒绝，业务码 {code}')
        page.on('request', started)
        page.on('response', received)

    def reconcile(self, info):
        if (not isinstance(info, dict) or info.get('platform') != 'wechat_channels'
                or info.get('account_id') != self.account.account_id or info.get('submitted') is not True
                or info.get('nickname') != self.account.platform_nickname or not self.account.platform_nickname):
            return False
        proof = info.get('confirmation')
        if not isinstance(proof, dict):
            return False
        if proof.get('kind') == 'success_notice':
            return bool(SUCCESS_TEXT.fullmatch(str(proof.get('text', '')).strip()))
        return (proof.get('kind') == 'wechat_publish_response' and proof.get('accepted') is True
                and type(proof.get('business_code')) is int and proof['business_code'] == 0
                and publish_endpoint(proof.get('endpoint', '')))
